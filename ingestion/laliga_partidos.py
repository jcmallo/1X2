"""
Ingesta oficial de Primera y Segunda División desde laliga.com.

Un único proveedor sirve para:
- primera  -> LaLiga / LALIGA EA SPORTS
- segunda  -> Segunda División / LALIGA HYPERMOTION

Modos:
- auto: incremental; la base decide qué jornadas revisar.
- reconciliar: auto + unas pocas jornadas recientes.
- completo: recorre toda la competición (backfill manual).
- LALIGA_JORNADAS="1,2,3": fuerza jornadas concretas.

Si una competición está todavía vacía, el modo auto hace un bootstrap de
las primeras jornadas para alcanzar rápidamente la zona actual y, desde ahí,
pasa a funcionamiento incremental.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta


BASE = "https://www.laliga.com"
FUENTE = "laliga.com"
TEMPORADA = "2026-27"
TEMPORADA_FECHA_INICIO = "2026-08-01"
TEMPORADA_FECHA_FIN = "2027-06-30"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Quiniela1X2/1.0; "
        "+https://1x2.juancarlosmallo.com)"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

DIVISIONES = {
    "primera": {
        "slug": "laliga-easports",
        "match_slug": "laliga-ea-sports",
        "nombre": "LaLiga",
        "marca": "LALIGA EA SPORTS",
        "nivel": "PRIMERA_CATEGORIA",
        "numero_equipos": 20,
        "numero_jornadas": 38,
        "fecha_inicio": "2026-08-15",
        "fecha_fin": "2027-05-24",
        "bootstrap_jornadas": 8,
    },
    "segunda": {
        "slug": "laliga-hypermotion",
        "match_slug": "laliga-hypermotion",
        "nombre": "Segunda División",
        "marca": "LALIGA HYPERMOTION",
        "nivel": "SEGUNDA_CATEGORIA",
        "numero_equipos": 22,
        "numero_jornadas": 42,
        "fecha_inicio": "2026-08-14",
        # La fecha final exacta puede fijarse más adelante desde calendario
        # oficial; NULL es válido en temporada_competicion.
        "fecha_fin": None,
        "bootstrap_jornadas": 8,
    },
}


def ahora_sql() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def simplificar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.casefold()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def slug_fallback(texto: str) -> str:
    return simplificar(texto).replace(" ", "-")


def get_con_reintentos(
    session: requests.Session,
    url: str,
) -> requests.Response:
    ultimo = None
    for intento in range(1, 4):
        try:
            r = session.get(url, timeout=(10, 40), allow_redirects=True)
            if r.status_code == 200:
                return r
            if r.status_code in {429, 500, 502, 503, 504}:
                ultimo = RuntimeError(f"HTTP {r.status_code}")
                if intento < 3:
                    time.sleep(2 ** (intento - 1))
                    continue
            r.raise_for_status()
        except (requests.Timeout, requests.ConnectionError) as exc:
            ultimo = exc
            if intento < 3:
                time.sleep(2 ** (intento - 1))
                continue
    raise RuntimeError(f"No se pudo descargar {url}: {ultimo}")


def division_config() -> tuple[str, dict]:
    division = os.environ.get("LALIGA_DIVISION", "primera").strip().lower()
    if division not in DIVISIONES:
        raise RuntimeError("LALIGA_DIVISION debe ser primera o segunda.")
    return division, DIVISIONES[division]


def jornadas_desde_texto(raw: str, max_jornada: int) -> list[int]:
    resultado = []
    for parte in raw.split(","):
        parte = parte.strip()
        if not parte:
            continue
        n = int(parte)
        if not 1 <= n <= max_jornada:
            raise RuntimeError(
                f"Las jornadas deben estar entre 1 y {max_jornada}."
            )
        resultado.append(n)
    if not resultado:
        raise RuntimeError("La lista de jornadas está vacía.")
    return sorted(set(resultado))


def jornadas_configuradas(
    api: ApiIngesta,
    cfg: dict,
) -> tuple[list[int], str]:
    max_total = int(cfg["numero_jornadas"])

    raw = os.environ.get("LALIGA_JORNADAS", "").strip()
    if raw:
        return jornadas_desde_texto(raw, max_total), "manual"

    modo = os.environ.get("LALIGA_MODO", "auto").strip().lower()

    if modo == "completo":
        return list(range(1, max_total + 1)), "completo"

    if modo not in {"auto", "reconciliar"}:
        raise RuntimeError(
            "LALIGA_MODO debe ser auto, reconciliar o completo."
        )

    contexto = api.contexto_partidos(
        competicion=cfg["nombre"],
        temporada=TEMPORADA,
        genero="MASCULINO",
        retro_horas=int(os.environ.get("LALIGA_RETRO_HORAS", "72")),
        futuro_dias=int(os.environ.get("LALIGA_FUTURO_DIAS", "21")),
        adelante_jornadas=int(
            os.environ.get("LALIGA_ADELANTE_JORNADAS", "3")
        ),
        reconciliar=(modo == "reconciliar"),
        ultimas_jornadas=int(
            os.environ.get("LALIGA_ULTIMAS_JORNADAS", "4")
        ),
    )

    max_cargada = int(contexto.get("max_jornada_cargada") or 0)

    # Primera ejecución: pequeño backfill automático. No hace falta pedir al
    # usuario que cambie manualmente a modo completo.
    if max_cargada == 0:
        n = min(int(cfg["bootstrap_jornadas"]), max_total)
        jornadas = list(range(1, n + 1))
        print(
            f"Bootstrap automático: competición vacía -> jornadas {jornadas}"
        )
        return jornadas, "bootstrap"

    jornadas = [
        int(j)
        for j in contexto.get("jornadas", [])
        if 1 <= int(j) <= max_total
    ]
    jornadas = sorted(set(jornadas))

    if not jornadas:
        desde = max(1, max_cargada - 1)
        hasta = min(max_total, max_cargada + 3)
        jornadas = list(range(desde, hasta + 1))

    print(
        "Contexto automático -> "
        f"max_cargada={max_cargada}, jornadas={jornadas}"
    )
    return jornadas, modo


def descubrir_partidos(html: str, cfg: dict) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    encontrados = {}

    prefijo = f"temporada-2026-2027-{cfg['match_slug']}-"

    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if "/partido/" not in href:
            continue

        abs_url = urljoin(BASE, href)
        path = urlparse(abs_url).path.rstrip("/")
        ident = path.split("/")[-1]

        if not ident.startswith(prefijo):
            continue

        encontrados[ident] = abs_url

    return list(encontrados.values())


def parse_titulo(soup: BeautifulSoup, cfg: dict) -> tuple[str, str]:
    titulo = soup.title.get_text(" ", strip=True) if soup.title else ""

    m = re.search(
        r"^(.*?)\s+vs\s+(.*?)\s+-\s+LALIGA\b",
        titulo,
        re.I,
    )
    if not m:
        # Algunas variantes traducidas pueden usar "Vs".
        m = re.search(
            rf"^(.*?)\s+Vs\s+(.*?)\s+-\s+{re.escape(cfg['marca'])}\b",
            titulo,
            re.I,
        )

    if not m:
        raise RuntimeError(f"No pude extraer equipos del título: {titulo!r}")

    return m.group(1).strip(), m.group(2).strip()


def id_equipo_desde_enlaces(
    soup: BeautifulSoup,
    nombre: str,
) -> str:
    objetivo = simplificar(nombre)
    candidatos = []

    for a in soup.find_all("a", href=True):
        texto = a.get_text(" ", strip=True)
        if not texto:
            continue

        href = str(a["href"])
        path = urlparse(urljoin(BASE, href)).path
        m = re.search(r"/clubes/([^/?#]+)", path, re.I)
        if not m:
            continue

        slug = m.group(1).strip().lower()
        candidatos.append((texto, slug))

        if simplificar(texto) == objetivo:
            return slug

    # Fallback: coincidencia parcial prudente.
    for texto, slug in candidatos:
        a = simplificar(texto)
        if a and objetivo and (a in objetivo or objetivo in a):
            return slug

    # Nunca dejamos el ID vacío. El slug derivado del nombre sigue siendo
    # estable dentro de nuestra fuente, aunque preferimos siempre el href.
    return slug_fallback(nombre)


def localizar_cabecera(
    soup: BeautifulSoup,
    local: str,
    visitante: str,
) -> tuple[list[str], int, int]:
    cadenas = [s.strip() for s in soup.stripped_strings if s.strip()]
    slocal = simplificar(local)
    svisit = simplificar(visitante)

    for i, s in enumerate(cadenas):
        if simplificar(s) != slocal:
            continue

        for j in range(i + 1, min(len(cadenas), i + 16)):
            if simplificar(cadenas[j]) == svisit:
                return cadenas, i, j

    raise RuntimeError(
        f"No pude localizar la cabecera de {local} vs {visitante}."
    )


def parse_estado(segmento: list[str]) -> str:
    low = simplificar(" ".join(segmento[:35]))

    if "finalizado" in low:
        return "FINALIZADO"
    if "aplazado" in low:
        return "APLAZADO"
    if "suspendido" in low:
        return "SUSPENDIDO"
    if "cancelado" in low:
        return "CANCELADO"
    if (
        "en juego" in low
        or "en directo" in low
        or "descanso" in low
        or "directo" in low
    ):
        return "EN_JUEGO"
    return "PROGRAMADO"


def parse_marcador(
    cadenas: list[str],
    i_local: int,
    i_visitante: int,
    estado: str,
) -> tuple[int | None, int | None]:
    entre = cadenas[i_local + 1:i_visitante]

    numeros = []
    for token in entre:
        limpio = token.strip()
        if re.fullmatch(r"\d{1,2}", limpio):
            numeros.append(int(limpio))

    if estado in {"FINALIZADO", "EN_JUEGO"} and len(numeros) >= 2:
        return numeros[0], numeros[-1]

    if estado == "FINALIZADO":
        raise RuntimeError("Partido finalizado pero no pude leer el marcador.")

    return None, None


def parse_fecha_hora_y_estadio(
    cadenas: list[str],
    i_visitante: int,
) -> tuple[str, str | None]:
    fin = min(len(cadenas), i_visitante + 70)
    segmento = cadenas[i_visitante:fin]

    idx_fecha = None
    fecha = None

    patron_fecha = re.compile(
        r"(?:LUN|MAR|MI[ÉE]|JUE|VIE|S[ÁA]B|DOM)\s+"
        r"(\d{1,2})\.(\d{1,2})\.(\d{4})",
        re.I,
    )

    for idx, token in enumerate(segmento):
        m = patron_fecha.search(token)
        if m:
            idx_fecha = idx
            fecha = (
                f"{int(m.group(3)):04d}-"
                f"{int(m.group(2)):02d}-"
                f"{int(m.group(1)):02d}"
            )
            break

    if idx_fecha is None or fecha is None:
        raise RuntimeError(
            "La ficha todavía no publica una fecha exacta; no se inventa."
        )

    idx_hora = None
    hora = None
    patron_hora = re.compile(r"\b(\d{1,2}):(\d{2})\s*h?\b", re.I)

    for idx in range(idx_fecha, min(len(segmento), idx_fecha + 12)):
        m = patron_hora.search(segmento[idx])
        if m:
            idx_hora = idx
            hora = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}:00"
            break

    if idx_hora is None or hora is None:
        raise RuntimeError(
            "La ficha todavía no publica una hora exacta; no se inventa."
        )

    estadio = None

    # En la ficha oficial el estadio aparece inmediatamente después de la
    # hora. Filtramos números de asistencia y etiquetas auxiliares.
    for token in segmento[idx_hora + 1:idx_hora + 8]:
        t = token.strip()
        st = simplificar(t)
        if not t:
            continue
        if re.fullmatch(r"[\d\.\,\s]+\*?", t):
            continue
        # LALIGA suele separar "19:00 h" en dos strings: "19:00" y "h".
        # Esa "h" NO es el estadio.
        if st in {
            "h",
            "horario peninsular",
            "jornada",
            "previa",
            "alineaciones",
            "minuto a minuto",
            "estadisticas",
        }:
            continue
        if re.fullmatch(r"jornada\s+\d+", st):
            continue
        if len(t) <= 2:
            continue
        estadio = t
        break

    return f"{fecha} {hora}", estadio


def parse_jornada(
    cadenas: list[str],
    i_visitante: int,
    jornada_esperada: int,
) -> int:
    segmento = " ".join(
        cadenas[i_visitante:min(len(cadenas), i_visitante + 90)]
    )
    m = re.search(r"\bJornada\s+(\d+)\b", segmento, re.I)
    if m:
        return int(m.group(1))
    return jornada_esperada


def parse_partido(
    url: str,
    html: str,
    jornada_esperada: int,
    cfg: dict,
) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    path = urlparse(url).path.rstrip("/")
    id_partido = path.split("/")[-1]
    if not id_partido.startswith("temporada-2026-2027-"):
        raise RuntimeError("No pude obtener el ID oficial del partido.")

    local, visitante = parse_titulo(soup, cfg)
    cadenas, i_local, i_visitante = localizar_cabecera(
        soup, local, visitante
    )

    estado = parse_estado(cadenas[i_local:i_visitante + 45])
    goles_local, goles_visitante = parse_marcador(
        cadenas, i_local, i_visitante, estado
    )
    fecha_hora, estadio = parse_fecha_hora_y_estadio(
        cadenas, i_visitante
    )
    jornada = parse_jornada(
        cadenas, i_visitante, jornada_esperada
    )

    return {
        "fuente": FUENTE,
        "id_partido_fuente": id_partido,
        "competicion_nombre": cfg["nombre"],
        "competicion_pais": "España",
        "competicion_genero": "MASCULINO",
        "competicion_nivel": cfg["nivel"],
        "competicion_organizador": "LaLiga",
        "competicion_apta_quiniela": 1,
        "competicion_numero_equipos": cfg["numero_equipos"],
        "competicion_numero_jornadas": cfg["numero_jornadas"],
        "competicion_formato": "liga regular ida y vuelta",
        "competicion_fecha_inicio": cfg["fecha_inicio"],
        "competicion_fecha_fin": cfg["fecha_fin"],
        "temporada_etiqueta": TEMPORADA,
        "temporada_fecha_inicio": TEMPORADA_FECHA_INICIO,
        "temporada_fecha_fin": TEMPORADA_FECHA_FIN,
        "jornada_numero": jornada,
        "fecha_hora_inicio": fecha_hora,
        "estado": estado,
        "goles_local": goles_local,
        "goles_visitante": goles_visitante,
        "local": {
            "nombre": local,
            "id_fuente": id_equipo_desde_enlaces(soup, local),
        },
        "visitante": {
            "nombre": visitante,
            "id_fuente": id_equipo_desde_enlaces(soup, visitante),
        },
        "estadio_nombre": estadio,
        "tipo_contenido_raw": f"partido_{cfg['slug']}",
        "documento_url": url,
        "contenido_raw": html,
        "obtenido_en": ahora_sql(),
    }


def main() -> None:
    division, cfg = division_config()

    api = ApiIngesta()
    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )
    print(
        f"Competición: {division} -> {cfg['nombre']} / {cfg['marca']}"
    )

    jornadas, modo = jornadas_configuradas(api, cfg)
    print("Modo:", modo)
    print("Jornadas a procesar:", jornadas)

    lote = api.iniciar_lote(
        fuente=FUENTE,
        tipo_fuente="scraping",
        notas=(
            f"{cfg['nombre']} {TEMPORADA}; "
            f"modo={modo}; jornadas={jornadas}"
        ),
    )
    print("Lote abierto:", lote)

    session = requests.Session()
    session.headers.update(HEADERS)

    creados = 0
    actualizados = 0
    omitidos = 0
    errores = []

    for jornada in jornadas:
        url_jornada = (
            f"{BASE}/{cfg['slug']}/resultados/"
            f"{TEMPORADA}/jornada-{jornada}"
        )
        print(f"\nJornada {jornada}: {url_jornada}")

        try:
            r = get_con_reintentos(session, url_jornada)
            html_jornada = r.text

            api.guardar_documento({
                "lote_id": lote,
                "fuente": FUENTE,
                "url": url_jornada,
                "tipo_contenido": f"resultados_{cfg['slug']}",
                "obtenido_en": ahora_sql(),
                "contenido": html_jornada,
            })

            urls = descubrir_partidos(html_jornada, cfg)
            print("  fichas descubiertas:", len(urls))

            # Una jornada oficial debería tener al menos una ficha. Si no,
            # puede ser una jornada todavía no publicada; se omite sin
            # inventar datos.
            if not urls:
                omitidos += 1
                print("  OMITIDA: sin fichas oficiales publicadas.")
                continue

            for url_partido in urls:
                time.sleep(0.40)
                try:
                    rp = get_con_reintentos(session, url_partido)
                    payload = parse_partido(
                        url_partido,
                        rp.text,
                        jornada,
                        cfg,
                    )
                    payload["lote_id"] = lote

                    if int(payload["jornada_numero"]) != jornada:
                        omitidos += 1
                        print(
                            "  OMITIDO por jornada inesperada:",
                            url_partido,
                        )
                        continue

                    res = api.guardar_partido(payload)

                    if res.get("accion") == "creado":
                        creados += 1
                    else:
                        actualizados += 1

                    print(
                        f"  {payload['local']['nombre']} - "
                        f"{payload['visitante']['nombre']} -> "
                        f"{res.get('accion')} "
                        f"partido_id={res.get('partido_id')}"
                    )

                except Exception as exc:
                    msg = str(exc)
                    if (
                        "no publica una hora exacta" in msg
                        or "no publica una fecha exacta" in msg
                    ):
                        omitidos += 1
                        print("  OMITIDO (sin fecha/hora oficial):", url_partido)
                    else:
                        errores.append(f"{url_partido}: {exc}")
                        print("  ERROR:", url_partido, "->", exc)

        except Exception as exc:
            errores.append(f"{url_jornada}: {exc}")
            print("  ERROR jornada:", exc)

    if errores and (creados or actualizados):
        estado = "parcial"
    elif errores:
        estado = "error"
    else:
        estado = "completado"

    notas = (
        f"division={division}; creados={creados}; "
        f"actualizados={actualizados}; omitidos={omitidos}; "
        f"errores={len(errores)}"
    )
    if errores:
        notas += " | " + " | ".join(errores[:3])

    api.finalizar_lote(
        lote,
        estado=estado,
        notas=notas,
    )

    print(
        f"\nResumen {division}: creados={creados}, "
        f"actualizados={actualizados}, omitidos={omitidos}, "
        f"errores={len(errores)}"
    )

    if errores:
        raise RuntimeError(
            f"La ingesta de {division} terminó con "
            f"{len(errores)} error(es). Primer error: {errores[0]}"
        )


if __name__ == "__main__":
    main()
