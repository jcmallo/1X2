"""
Ingesta incremental de partidos de Liga F desde la web oficial ligaf.es.

Primera puesta en marcha:
    jornadas 1 y 2 de la temporada 2026-27.

Guarda:
- HTML RAW de las páginas de resultados y de cada ficha de partido.
- competición/temporada si faltan.
- equipos FEMENINOS separados de los masculinos aunque compartan nombre.
- estadio cuando la ficha lo publica.
- partido + resultado/estado.
- IDs externos de equipos y partidos para que las siguientes ejecuciones
  actualicen y NO dupliquen.

No carga todavía alineaciones/estadísticas/eventos: eso será el siguiente bloque.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta


BASE = "https://ligaf.es"
FUENTE = "ligaf.es"
TEMPORADA = "2026-27"
TEMPORADA_FECHA_INICIO = "2026-08-29"
TEMPORADA_FECHA_FIN = "2027-05-23"

HEADERS = {
    "User-Agent": "quiniela-1x2/1.0 https://1x2.juancarlosmallo.com",
    "Accept-Language": "es-ES,es;q=0.9",
}

MESES = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
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


def get_con_reintentos(session: requests.Session, url: str) -> requests.Response:
    ultimo = None
    for intento in range(1, 4):
        try:
            r = session.get(url, timeout=(10, 35))
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


def jornadas_configuradas() -> list[int]:
    raw = os.environ.get("LIGAF_JORNADAS", "1,2").strip()
    resultado = []
    for parte in raw.split(","):
        parte = parte.strip()
        if not parte:
            continue
        n = int(parte)
        if not 1 <= n <= 30:
            raise RuntimeError("Las jornadas deben estar entre 1 y 30.")
        resultado.append(n)
    if not resultado:
        raise RuntimeError("LIGAF_JORNADAS está vacío.")
    return sorted(set(resultado))


def descubrir_partidos(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    encontrados = {}

    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "/partido/" not in href:
            continue
        m = re.search(r"/(\d+)(?:/(?:detalle|detail))?(?:[/?#]|$)", href, re.I)
        if not m:
            continue
        encontrados[m.group(1)] = urljoin(BASE, href)

    return list(encontrados.values())


def parse_titulo(soup: BeautifulSoup) -> tuple[str, str]:
    titulo = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = re.search(
        r"^(.*?)\s+Vs\s+(.*?)\s+-\s+Liga\s+F\b",
        titulo,
        re.I,
    )
    if not m:
        raise RuntimeError(f"No pude extraer equipos del título: {titulo!r}")
    return m.group(1).strip(), m.group(2).strip()


def ids_equipos(
    soup: BeautifulSoup,
    local: str,
    visitante: str,
) -> tuple[str, str]:
    candidatos = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        m = re.search(r"/equipo/[^/]+/(\d+)(?:/|$)", href, re.I)
        if not m:
            continue
        nombre = a.get_text(" ", strip=True)
        if nombre:
            candidatos.append((nombre, m.group(1)))

    def resolver(nombre_buscado: str):
        buscado = simplificar(nombre_buscado)
        for nombre, id_fuente in candidatos:
            if simplificar(nombre) == buscado:
                return id_fuente
        return None

    id_local = resolver(local)
    id_visitante = resolver(visitante)

    if id_local and id_visitante:
        return id_local, id_visitante

    # Fallback prudente: los primeros dos equipos únicos de la cabecera.
    unicos = []
    vistos = set()
    for nombre, ident in candidatos:
        if ident not in vistos:
            vistos.add(ident)
            unicos.append((nombre, ident))
    if len(unicos) >= 2:
        return id_local or unicos[0][1], id_visitante or unicos[1][1]

    raise RuntimeError(
        f"No pude resolver IDs oficiales de {local} / {visitante}"
    )


def parse_fecha_y_jornada(texto: str) -> tuple[int, str]:
    limpio = " ".join(texto.split())

    mj = re.search(r"JOR\.?\s*(\d+)", limpio, re.I)
    if not mj:
        raise RuntimeError("No pude localizar la jornada en la ficha.")
    jornada = int(mj.group(1))

    # Ejemplos oficiales: "Sáb 29 AGO 12:00", "Dom 30 AGO 21:00".
    mf = re.search(
        r"(?:LUN|MAR|MI[ÉE]|JUE|VIE|S[ÁA]B|DOM)"
        r"\s+(\d{1,2})\s+"
        r"(ENE|FEB|MAR|ABR|MAY|JUN|JUL|AGO|SEP|OCT|NOV|DIC)"
        r"\s+(\d{2}:\d{2})",
        limpio.upper(),
    )
    if not mf:
        raise RuntimeError(
            "La ficha todavía no publica una hora exacta; no se inventa."
        )

    dia = int(mf.group(1))
    mes = MESES[mf.group(2)]
    hora = mf.group(3)

    # Temporada 2026-27: agosto-diciembre son 2026; enero-julio, 2027.
    anio = 2026 if mes >= 8 else 2027
    return jornada, f"{anio:04d}-{mes:02d}-{dia:02d} {hora}:00"


def parse_estado_y_resultado(
    soup: BeautifulSoup,
    texto: str,
) -> tuple[str, int | None, int | None]:
    low = simplificar(texto)

    if "finalizado" in low:
        estado = "FINALIZADO"
    elif "aplazado" in low:
        estado = "APLAZADO"
    elif "suspendido" in low:
        estado = "SUSPENDIDO"
    elif "cancelado" in low:
        estado = "CANCELADO"
    elif "en juego" in low:
        estado = "EN_JUEGO"
    else:
        estado = "PROGRAMADO"

    if estado == "FINALIZADO":
        nodo = soup.find(
            string=re.compile(r"^\s*\d{1,2}\s*-\s*\d{1,2}\s*$")
        )
        if nodo:
            m = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", str(nodo))
            if m:
                return estado, int(m.group(1)), int(m.group(2))

        # Fallback sobre el texto completo: la cabecera aparece antes que
        # la clasificación incluida al final de la ficha.
        m = re.search(r"\b(\d{1,2})\s*-\s*(\d{1,2})\b", texto)
        if m:
            return estado, int(m.group(1)), int(m.group(2))

        raise RuntimeError("Partido finalizado pero no pude leer el marcador.")

    return estado, None, None


def parse_estadio(soup: BeautifulSoup) -> str | None:
    for img in soup.find_all("img"):
        alt = str(img.get("alt") or "").strip()
        if simplificar(alt).startswith("estadio "):
            nombre = re.sub(r"^Estadio\s+", "", alt, flags=re.I).strip()
            return nombre or None
    return None


def parse_partido(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)

    mid = re.search(r"/(\d+)(?:/(?:detalle|detail))?(?:[/?#]|$)", url, re.I)
    if not mid:
        raise RuntimeError("No pude obtener el ID oficial del partido.")

    local, visitante = parse_titulo(soup)
    id_local, id_visitante = ids_equipos(soup, local, visitante)
    jornada, fecha = parse_fecha_y_jornada(texto)
    estado, goles_local, goles_visitante = parse_estado_y_resultado(
        soup, texto
    )

    return {
        "fuente": FUENTE,
        "id_partido_fuente": mid.group(1),
        "competicion_nombre": "Liga F",
        "temporada_etiqueta": TEMPORADA,
        "temporada_fecha_inicio": TEMPORADA_FECHA_INICIO,
        "temporada_fecha_fin": TEMPORADA_FECHA_FIN,
        "jornada_numero": jornada,
        "fecha_hora_inicio": fecha,
        "estado": estado,
        "goles_local": goles_local,
        "goles_visitante": goles_visitante,
        "local": {
            "nombre": local,
            "id_fuente": id_local,
        },
        "visitante": {
            "nombre": visitante,
            "id_fuente": id_visitante,
        },
        "estadio_nombre": parse_estadio(soup),
        "documento_url": url,
        "contenido_raw": html,
        "obtenido_en": ahora_sql(),
    }


def main() -> None:
    api = ApiIngesta()
    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    jornadas = jornadas_configuradas()
    print("Jornadas a procesar:", jornadas)

    lote = api.iniciar_lote(
        fuente=FUENTE,
        tipo_fuente="scraping",
        notas=f"Liga F {TEMPORADA}; jornadas {jornadas}",
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
            f"{BASE}/resultados/primera_division_femenina/{jornada}"
        )
        print(f"\nJornada {jornada}: {url_jornada}")

        try:
            r = get_con_reintentos(session, url_jornada)
            html_jornada = r.text

            api.guardar_documento({
                "lote_id": lote,
                "fuente": FUENTE,
                "url": url_jornada,
                "tipo_contenido": "resultados_ligaf",
                "obtenido_en": ahora_sql(),
                "contenido": html_jornada,
            })

            urls = descubrir_partidos(html_jornada)
            print("  fichas descubiertas:", len(urls))

            for url_partido in urls:
                time.sleep(0.35)
                try:
                    rp = get_con_reintentos(session, url_partido)
                    payload = parse_partido(url_partido, rp.text)
                    payload["lote_id"] = lote

                    # Protección por si la página enlaza accidentalmente otra jornada.
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
                        f"{res.get('accion')} partido_id={res.get('partido_id')}"
                    )

                except Exception as exc:
                    # Si aún no hay hora oficial, no inventamos 12:00.
                    if "no publica una hora exacta" in str(exc):
                        omitidos += 1
                        print("  OMITIDO (sin hora oficial):", url_partido)
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
        f"creados={creados}; actualizados={actualizados}; "
        f"omitidos={omitidos}; errores={len(errores)}"
    )
    if errores:
        notas += " | " + " | ".join(errores[:3])

    api.finalizar_lote(lote, estado=estado, notas=notas)

    print(
        f"\nResumen: creados={creados}, actualizados={actualizados}, "
        f"omitidos={omitidos}, errores={len(errores)}"
    )

    if errores:
        raise RuntimeError(
            f"La ingesta terminó con {len(errores)} error(es). "
            f"Primer error: {errores[0]}"
        )


if __name__ == "__main__":
    main()
