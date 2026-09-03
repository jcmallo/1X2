"""
Carga las plantillas actuales 2026-27 de:
- LALIGA EA SPORTS
- LALIGA HYPERMOTION
- Liga F

Fuentes oficiales:
- laliga.com -> página /clubes/<slug>/estadisticas
  La tabla oficial incluye dorsal, posición y jugadores registrados,
  también jugadores con 0 minutos.
- ligaf.es -> /equipo/<slug>/<id>/plantilla/2027
  La plantilla oficial incluye ID de jugadora, nombre, posición,
  nacionalidad, nacimiento y altura.

No inventa jugadores. Si un parser obtiene menos de 15 futbolistas,
la plantilla se rechaza para evitar cerrar pertenencias por error.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta


TEMPORADA = "2026-27"
LIGAF_ANIO_URL = "2027"

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (compatible; Quiniela1X2/1.0; "
            "+https://1x2.juancarlosmallo.com)"
        ),
        "Accept-Language": "es-ES,es;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
)


def ahora_sql() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def slugify(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", valor)
    texto = "".join(
        c for c in texto
        if not unicodedata.combining(c)
    ).lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto


def pedir_html(url: str, intentos: int = 3) -> tuple[str, str]:
    ultimo = None

    for intento in range(1, intentos + 1):
        try:
            r = session.get(url, timeout=(10, 45))
            if r.status_code == 200:
                return r.text, r.url

            if r.status_code in {429, 500, 502, 503, 504} and intento < intentos:
                espera = 2 ** (intento - 1)
                print(
                    f"    HTTP {r.status_code}; "
                    f"reintento en {espera}s..."
                )
                time.sleep(espera)
                continue

            r.raise_for_status()

        except (requests.Timeout, requests.ConnectionError) as exc:
            ultimo = exc
            if intento < intentos:
                espera = 2 ** (intento - 1)
                time.sleep(espera)
                continue
            raise

    raise RuntimeError(f"No se pudo descargar {url}: {ultimo}")


def guardar_raw(
    api: ApiIngesta,
    lote_id: int,
    fuente: str,
    url: str,
    tipo: str,
    html: str,
) -> None:
    api.guardar_documento(
        {
            "lote_id": lote_id,
            "fuente": fuente,
            "url": url,
            "tipo_contenido": tipo,
            "obtenido_en": ahora_sql(),
            "contenido": html,
            "hash_contenido": hashlib.sha256(
                html.encode("utf-8")
            ).hexdigest(),
        }
    )


def normalizar_posicion_laliga(valor: str | None) -> str | None:
    if not valor:
        return None

    v = valor.strip().casefold()

    if v in {"portero", "portera", "goalkeeper"}:
        return "Portero"
    if v in {"defensa", "defender"}:
        return "Defensa"
    if v in {
        "centrocampista",
        "centro",
        "medio",
        "midfielder",
    }:
        return "Centrocampista"
    if v in {
        "delantero",
        "delantera",
        "forward",
    }:
        return "Delantero"

    return valor.strip()[:30]


def extraer_dorsal(strings: list[str], limite: int | None = None) -> int | None:
    zona = strings if limite is None else strings[:limite]

    for s in zona:
        if re.fullmatch(r"\d{1,2}", s.strip()):
            n = int(s)
            if 0 <= n <= 99:
                return n
    return None


def parse_laliga(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jugadores: dict[str, dict] = {}

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        m = re.search(r"/jugador/([^/?#]+)", href)
        if not m:
            continue

        id_fuente = m.group(1).strip()
        if not id_fuente:
            continue

        tr = a.find_parent("tr")
        if tr is None:
            continue

        strings = [s.strip() for s in tr.stripped_strings if s.strip()]
        if not strings:
            continue

        posicion = None
        pos_idx = None
        for i, s in enumerate(strings):
            p = normalizar_posicion_laliga(s)
            if p in {
                "Portero",
                "Defensa",
                "Centrocampista",
                "Delantero",
            }:
                posicion = p
                pos_idx = i
                break

        if posicion is None:
            continue

        nombre = " ".join(a.stripped_strings).strip()

        # Algunas celdas enlazan una imagen y dejan el nombre como texto
        # del td. En ese caso, el nombre suele estar justo después de posición.
        if not nombre or len(nombre) < 2:
            if pos_idx is not None and pos_idx + 1 < len(strings):
                nombre = strings[pos_idx + 1].strip()

        if not nombre or nombre.casefold() in {
            "ver jugador",
            "ficha",
        }:
            continue

        dorsal = extraer_dorsal(strings, pos_idx)

        existente = jugadores.get(id_fuente)
        datos = {
            "id_fuente": id_fuente,
            "nombre_completo": nombre[:150],
            "fecha_nacimiento": None,
            "nacionalidad": None,
            "altura_cm": None,
            "posicion_principal": posicion,
            "dorsal": dorsal,
        }

        if existente is None:
            jugadores[id_fuente] = datos
        else:
            if existente.get("dorsal") is None and dorsal is not None:
                existente["dorsal"] = dorsal
            if existente.get("posicion_principal") is None:
                existente["posicion_principal"] = posicion

    salida = list(jugadores.values())

    if len(salida) < 15:
        raise RuntimeError(
            f"Parser LALIGA solo encontró {len(salida)} jugadores."
        )

    return salida


def siguiente_valor(strings: list[str], etiqueta: str) -> str | None:
    objetivo = etiqueta.casefold()

    for i, s in enumerate(strings):
        if s.casefold().strip(": ") == objetivo:
            for candidato in strings[i + 1:]:
                c = candidato.strip()
                if c:
                    return c
    return None


def parse_fecha_es(valor: str | None) -> str | None:
    if not valor:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", valor)
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def parse_altura(valor: str | None) -> int | None:
    if not valor:
        return None
    m = re.search(r"(\d{3})\s*cm", valor, re.I)
    if not m:
        return None
    n = int(m.group(1))
    return n if 130 <= n <= 230 else None


def parse_ligaf(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jugadores: dict[str, dict] = {}

    posiciones = {
        "portera": "Portero",
        "portero": "Portero",
        "defensa": "Defensa",
        "centro": "Centrocampista",
        "centrocampista": "Centrocampista",
        "delantera": "Delantero",
        "delantero": "Delantero",
    }

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        m = re.search(r"/jugadora/[^/?#]+/(\d+)", href)
        if not m:
            continue

        id_fuente = m.group(1)
        strings = [s.strip() for s in a.stripped_strings if s.strip()]

        if len(strings) < 3:
            continue

        pos_idx = None
        posicion = None

        for i, s in enumerate(strings):
            clave = s.casefold()
            if clave in posiciones:
                pos_idx = i
                posicion = posiciones[clave]
                break

        if pos_idx is None or posicion is None:
            continue

        previos = strings[:pos_idx]

        # Quitar dorsal si viene como elemento independiente.
        previos_sin_dorsal = [
            s for s in previos
            if not re.fullmatch(r"\d{1,2}", s)
        ]

        if not previos_sin_dorsal:
            continue

        # En las fichas LPFF suele venir:
        # nombre corto + nombre completo + posición.
        # El nombre completo es normalmente el fragmento más descriptivo.
        nombre = max(
            previos_sin_dorsal,
            key=lambda s: (len(s.split()), len(s)),
        ).strip()

        dorsal = extraer_dorsal(previos)

        nacionalidad = siguiente_valor(strings, "Nacionalidad")
        nacimiento = parse_fecha_es(
            siguiente_valor(strings, "Nacimiento")
        )
        altura = parse_altura(
            siguiente_valor(strings, "Altura")
        )

        jugadores[id_fuente] = {
            "id_fuente": id_fuente,
            "nombre_completo": nombre[:150],
            "fecha_nacimiento": nacimiento,
            "nacionalidad": nacionalidad[:80] if nacionalidad else None,
            "altura_cm": altura,
            "posicion_principal": posicion,
            "dorsal": dorsal,
        }

    salida = list(jugadores.values())

    if len(salida) < 15:
        raise RuntimeError(
            f"Parser Liga F solo encontró {len(salida)} jugadoras."
        )

    return salida


def slug_laliga(item: dict) -> str | None:
    for campo in ("id_laliga_com", "id_laliga_legacy"):
        valor = item.get(campo)
        if valor:
            return str(valor).strip()
    return None


def procesar_laliga(
    api: ApiIngesta,
    lote_id: int,
    item: dict,
) -> int:
    slug = slug_laliga(item)
    if not slug:
        raise RuntimeError(
            "El equipo no tiene ID externo laliga.com/laliga."
        )

    url = f"https://www.laliga.com/clubes/{slug}/estadisticas"
    html, final_url = pedir_html(url)

    guardar_raw(
        api,
        lote_id,
        "laliga.com",
        final_url,
        "plantilla_laliga",
        html,
    )

    jugadores = parse_laliga(html)

    res = api.guardar_plantilla(
        {
            "lote_id": lote_id,
            "equipo_id": int(item["equipo_id"]),
            "fuente": "laliga.com",
            "fecha_plantilla": date.today().isoformat(),
            "jugadores": jugadores,
        }
    )

    print(
        f"    {len(jugadores)} jugadores | "
        f"creados={res.get('jugadores_creados')} | "
        f"vínculos nuevos={res.get('pertenencias_nuevas')}"
    )

    return len(jugadores)


def procesar_ligaf(
    api: ApiIngesta,
    lote_id: int,
    item: dict,
) -> int:
    team_id = str(item.get("id_ligaf") or "").strip()
    if not team_id:
        raise RuntimeError("El equipo no tiene ID externo ligaf.es.")

    slug = slugify(str(item["nombre_canonico"]))
    url = (
        f"https://ligaf.es/equipo/{slug}/{team_id}/"
        f"plantilla/{LIGAF_ANIO_URL}"
    )

    html, final_url = pedir_html(url)

    guardar_raw(
        api,
        lote_id,
        "ligaf.es",
        final_url,
        "plantilla_ligaf",
        html,
    )

    jugadores = parse_ligaf(html)

    res = api.guardar_plantilla(
        {
            "lote_id": lote_id,
            "equipo_id": int(item["equipo_id"]),
            "fuente": "ligaf.es",
            "fecha_plantilla": date.today().isoformat(),
            "jugadores": jugadores,
        }
    )

    print(
        f"    {len(jugadores)} jugadoras | "
        f"creadas={res.get('jugadores_creados')} | "
        f"vínculos nuevos={res.get('pertenencias_nuevas')}"
    )

    return len(jugadores)


def main() -> None:
    api = ApiIngesta()

    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    equipos = api.contexto_plantillas(TEMPORADA)

    print("Equipos a procesar:", len(equipos))

    if not equipos:
        raise RuntimeError(
            "No hay equipos 2026-27 en contexto_plantillas."
        )

    lote_id = api.iniciar_lote(
        fuente="plantillas-oficiales-2026-27",
        tipo_fuente="scraping",
        notas=(
            "Plantillas oficiales LaLiga, Segunda y Liga F. "
            "Snapshot actual."
        ),
    )
    print("Lote abierto:", lote_id)

    equipos_ok = 0
    jugadores_total = 0
    errores: list[str] = []

    for item in equipos:
        nombre = str(item["nombre_canonico"])
        competicion = str(item["competicion"])

        print(f"- {competicion} | {nombre}")

        try:
            if competicion == "Liga F":
                n = procesar_ligaf(api, lote_id, item)
            else:
                n = procesar_laliga(api, lote_id, item)

            jugadores_total += n
            equipos_ok += 1

        except Exception as exc:
            errores.append(
                f"{competicion} | {nombre}: {exc}"
            )
            print("    ERROR:", exc)

        time.sleep(0.15)

    if errores and equipos_ok:
        estado = "parcial"
    elif errores:
        estado = "error"
    else:
        estado = "completado"

    notas = (
        f"equipos_ok={equipos_ok}; "
        f"jugadores={jugadores_total}; "
        f"errores={len(errores)}; "
        f"equipos_total={len(equipos)}"
    )

    if errores:
        notas += " | " + " | ".join(errores[:3])

    api.finalizar_lote(
        lote_id,
        estado=estado,
        notas=notas,
    )

    print(
        "\nResumen:"
        f" equipos_ok={equipos_ok}/{len(equipos)},"
        f" jugadores={jugadores_total},"
        f" errores={len(errores)}"
    )

    if errores:
        raise RuntimeError(
            f"Plantillas terminaron con {len(errores)} error(es). "
            f"Primer error: {errores[0]}"
        )


if __name__ == "__main__":
    main()
