"""
Backfill histórico de Liga F 2022-23 .. 2025-26 desde SoccerDonna.

SoccerDonna se usa porque sus fichas históricas conservan fecha/hora exacta,
marcador, estadio, árbitra y alineaciones/eventos. Este primer bloque normaliza
solo el PARTIDO en las tablas ya existentes y conserva el HTML completo en RAW
para poder añadir alineaciones/eventos después sin volver a descargarlo.

IMPORTANTE: las temporadas/equipos Liga F 2022-23..2026-27 deben existir ya
(carga_liga_f_2022_2027.sql). El script usa nombres canónicos para reutilizarlos
y no crear duplicados femeninos por diferencias de nomenclatura.
"""

from __future__ import annotations

import argparse
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta

BASE = "https://www.soccerdonna.de"
FUENTE = "soccerdonna.de"
COMPETICION = "Liga F"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Quiniela1X2-Historico/1.0; "
        "+https://1x2.juancarlosmallo.com)"
    ),
    "Accept-Language": "en-US,en;q=0.9,es;q=0.7",
}

# Nomenclaturas observadas en SoccerDonna -> canónicos ya usados por la BD.
CANONICOS = {
    "fc barcelona": "FC Barcelona Femení",
    "f c barcelona": "FC Barcelona Femení",
    "real madrid cf": "Real Madrid CF Femenino",
    "real madrid": "Real Madrid CF Femenino",
    "levante ud": "Levante UD Femenino",
    "club atletico de madrid": "Atlético de Madrid Femenino",
    "atletico de madrid": "Atlético de Madrid Femenino",
    "atletico mad": "Atlético de Madrid Femenino",
    "madrid cff": "Madrid CFF",
    "ud tenerife": "Costa Adeje Tenerife",
    "udg tenerife sur": "Costa Adeje Tenerife",
    "costa adeje tenerife": "Costa Adeje Tenerife",
    "cd tenerife femenino": "Costa Adeje Tenerife",  # nombre usado por SoccerDonna en 2025-26
    "tenerife fem": "Costa Adeje Tenerife",
    "sevilla fc": "Sevilla FC Femenino",
    "fc sevilla": "Sevilla FC Femenino",
    "f c sevilla": "Sevilla FC Femenino",
    "real sociedad": "Real Sociedad Femenino",
    "real socieadad": "Real Sociedad Femenino",  # typo histórico de SoccerDonna
    "valencia feminas cf": "Valencia CF Femenino",
    "valencia feminas club de futbol": "Valencia CF Femenino",
    "athletic club": "Athletic Club Femenino",
    "athletic bilbao": "Athletic Club Femenino",
    "fc levante las planas 2024": "FC Badalona Women",
    "fc levante las planas": "FC Badalona Women",
    "fc levante badalona": "FC Badalona Women",
    "fc badalona women": "FC Badalona Women",
    "sdf real betis balompie": "Real Betis Féminas",
    "real betis balompie": "Real Betis Féminas",
    "real betis feminas": "Real Betis Féminas",
    "cd sporting club de huelva": "Sporting Club Huelva",
    "sporting de huelva": "Sporting Club Huelva",
    "sporting club huelva": "Sporting Club Huelva",
    "sport huelva": "Sporting Club Huelva",
    "villarreal cf": "Villarreal CF Femenino",
    "alhama cf": "Alhama CF ElPozo",
    "alhama cf elpozo": "Alhama CF ElPozo",
    "deportivo alaves": "Deportivo Alavés Femenino",
    "deportivo alaves gloriosas": "Deportivo Alavés Femenino",
    "sd eibar": "SD Eibar Femenino",
    "sd eibar": "SD Eibar Femenino",
    "granada cf": "Granada CF Femenino",
    "fc domont": "Granada CF Femenino",  # SoccerDonna usa este nombre erróneo para el club ID de Granada
    "rcd espanyol": "RCD Espanyol Femenino",
    "espanyol barcelona": "RCD Espanyol Femenino",
    "rcd espanyol barcelona": "RCD Espanyol Femenino",
    "deportivo la coruna": "Deportivo Abanca",
    "rc deportivo a coruna": "Deportivo Abanca",
    "deportivo abanca": "Deportivo Abanca",
    "dux logrono": "Logroño United",
    "logrono united": "Logroño United",
    "edf logrono": "Logroño United",  # denominación histórica usada por SoccerDonna
}

PATRON_FICHA = re.compile(r"spielbericht_(\d+)\.html", re.I)
PATRON_EQUIPO_ID = re.compile(r"verein_(\d+)\.html", re.I)
PATRON_FECHA_HORA = re.compile(
    r"\b(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{1,2}):(\d{2})\b"
)
PATRON_JORNADA = re.compile(r"\b(\d{1,2})\.\s*Match\s+day\b", re.I)
PATRON_SCORE = re.compile(r"\b(\d{1,2}):(\d{1,2})\b")


def ahora_sql() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def simplificar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.casefold()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def canonico(nombre: str) -> str:
    clave = simplificar(nombre)
    if clave in CANONICOS:
        return CANONICOS[clave]
    raise RuntimeError(
        f"Nombre SoccerDonna sin mapa canónico: {nombre!r} (clave={clave!r}). "
        "Se aborta para no crear un equipo duplicado."
    )


def validar_temporada(etiqueta: str) -> tuple[int, int]:
    m = re.fullmatch(r"(20\d{2})-(\d{2})", etiqueta.strip())
    if not m:
        raise RuntimeError("Temporada inválida. Usa por ejemplo 2022-23.")
    inicio = int(m.group(1))
    fin = inicio + 1
    if fin % 100 != int(m.group(2)):
        raise RuntimeError("La temporada no es consecutiva: " + etiqueta)
    return inicio, fin


def jornadas_desde_texto(raw: str | None) -> list[int]:
    if not raw:
        return list(range(1, 31))
    salida: set[int] = set()
    for trozo in raw.split(","):
        trozo = trozo.strip()
        if not trozo:
            continue
        if "-" in trozo:
            a, b = map(int, trozo.split("-", 1))
            if a > b:
                a, b = b, a
            salida.update(range(a, b + 1))
        else:
            salida.add(int(trozo))
    if not salida or min(salida) < 1 or max(salida) > 30:
        raise RuntimeError("Jornadas Liga F válidas: 1..30.")
    return sorted(salida)


def get_con_reintentos(session: requests.Session, url: str) -> requests.Response:
    ultimo: Exception | None = None
    for intento in range(1, 4):
        try:
            r = session.get(url, timeout=(10, 45), allow_redirects=True)
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


def url_jornada(anio_inicio: int, jornada: int) -> str:
    return (
        f"{BASE}/en/primera-division-femenina/spieltagsuebersicht/"
        f"wettbewerb_ESP1_{anio_inicio}_{jornada}.html"
    )


def descubrir_fichas(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    encontrados: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        m = PATRON_FICHA.search(href)
        if not m:
            continue
        encontrados[m.group(1)] = urljoin(BASE, href)
    return list(encontrados.values())


def parse_titulo_equipos(soup: BeautifulSoup) -> tuple[str, str]:
    titulo = soup.title.get_text(" ", strip=True) if soup.title else ""
    # Ejemplo real:
    # Match report - Match report Club Atlético de Madrid - Real Socieadad,
    # 01.11.2022 - Primera División Femenina | Soccerdonna
    limpio = re.sub(r"\s*\|\s*Soccerdonna.*$", "", titulo, flags=re.I)
    m = re.search(
        r"Match report(?:\s*-\s*Match report)?\s+(.+?),\s*"
        r"\d{2}\.\d{2}\.\d{4}\s*-\s*Primera\s+Divisi[oó]n\s+Femenina",
        limpio,
        re.I,
    )
    if not m:
        raise RuntimeError(f"No pude interpretar título SoccerDonna: {titulo!r}")
    pareja = m.group(1).strip()
    if " - " not in pareja:
        raise RuntimeError(f"No pude separar local/visitante: {pareja!r}")
    local, visitante = pareja.split(" - ", 1)
    return local.strip(), visitante.strip()


def ids_equipos(soup: BeautifulSoup, local: str, visitante: str) -> tuple[str, str]:
    candidatos: list[tuple[str, str]] = []
    vistos: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        m = PATRON_EQUIPO_ID.search(href)
        if not m:
            continue
        ident = m.group(1)
        if ident in vistos:
            continue
        nombre = a.get_text(" ", strip=True)
        if not nombre:
            continue
        vistos.add(ident)
        candidatos.append((nombre, ident))

    def resolver(objetivo: str) -> str | None:
        so = simplificar(objetivo)
        for nombre, ident in candidatos:
            sn = simplificar(nombre)
            if sn == so or sn in so or so in sn:
                return ident
        return None

    id_local = resolver(local)
    id_visit = resolver(visitante)
    if id_local and id_visit and id_local != id_visit:
        return id_local, id_visit

    # En la ficha, los dos primeros IDs de club distintos son los equipos del partido.
    if len(candidatos) >= 2:
        return id_local or candidatos[0][1], id_visit or candidatos[1][1]
    raise RuntimeError("No pude resolver IDs de los dos clubes en SoccerDonna.")


def parse_estadio(soup: BeautifulSoup) -> str | None:
    texto = " ".join(soup.stripped_strings)
    # Segmento real: "Estadio Atlético de Madrid CD Alcalá de Henares - 1.200 Spectators"
    m = re.search(r"\bEstadio\s+(.+?)\s+-\s+[\d\.]+\s+Spectators\b", texto, re.I)
    if m:
        return m.group(1).strip()[:150] or None
    return None


def parse_partido(url: str, html: str, temporada: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    texto = " ".join(soup.stripped_strings)

    mid = PATRON_FICHA.search(url)
    if not mid:
        raise RuntimeError("No pude obtener spielbericht_id.")

    local_sd, visitante_sd = parse_titulo_equipos(soup)
    local = canonico(local_sd)
    visitante = canonico(visitante_sd)
    id_local, id_visitante = ids_equipos(soup, local_sd, visitante_sd)

    mf = PATRON_FECHA_HORA.search(texto)
    if not mf:
        raise RuntimeError("La ficha no contiene fecha/hora exacta.")
    dia, mes, anio, hora, minuto = map(int, mf.groups())

    mj = PATRON_JORNADA.search(texto)
    if not mj:
        raise RuntimeError("La ficha no contiene la jornada.")
    jornada = int(mj.group(1))

    # El marcador final aparece al principio de la ficha, ANTES de la fecha/hora.
    # No buscamos un N:N arbitrario: la hora (p. ej. 16:00), el descanso (0:0),
    # resultados de otros partidos y eventos de goles usan el mismo formato.
    texto_antes_fecha = texto[:mf.start()]
    ms = PATRON_SCORE.search(texto_antes_fecha)
    if not ms:
        raise RuntimeError("La ficha no contiene marcador final antes de la fecha/hora.")
    goles_local, goles_visitante = int(ms.group(1)), int(ms.group(2))

    return {
        "fuente": FUENTE,
        "id_partido_fuente": mid.group(1),
        "competicion_nombre": COMPETICION,
        "competicion_pais": "España",
        "competicion_genero": "FEMENINO",
        "competicion_nivel": "PRIMERA_CATEGORIA",
        "competicion_organizador": "Liga Profesional de Fútbol Femenino",
        "competicion_apta_quiniela": 1,
        "competicion_numero_equipos": 16,
        "competicion_numero_jornadas": 30,
        "competicion_formato": "liga regular ida y vuelta",
        # No enviamos fechas de temporada/competición: las temporadas Liga F ya
        # existen y así no sobrescribimos metadatos oficiales del seed.
        "temporada_etiqueta": temporada,
        "jornada_numero": jornada,
        "fecha_hora_inicio": f"{anio:04d}-{mes:02d}-{dia:02d} {hora:02d}:{minuto:02d}:00",
        "estado": "FINALIZADO",
        "goles_local": goles_local,
        "goles_visitante": goles_visitante,
        "local": {"nombre": local, "id_fuente": id_local},
        "visitante": {"nombre": visitante, "id_fuente": id_visitante},
        "estadio_nombre": parse_estadio(soup),
        "tipo_contenido_raw": "sd_ligaf_hist_partido",
        "documento_url": url,
        "contenido_raw": html,
        "obtenido_en": ahora_sql(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporada", required=True, help="Ej. 2022-23")
    parser.add_argument("--jornadas", help="Opcional: 1,2,5-8")
    parser.add_argument("--pausa", type=float, default=0.45)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    anio_inicio, _ = validar_temporada(args.temporada)
    jornadas = jornadas_desde_texto(args.jornadas)

    session = requests.Session()
    session.headers.update(HEADERS)

    if args.dry_run:
        total = 0
        for j in jornadas:
            url = url_jornada(anio_inicio, j)
            html = get_con_reintentos(session, url).text
            fichas = descubrir_fichas(html)
            print(f"J{j:02d}: {len(fichas)} fichas -> {url}")
            if len(fichas) != 8:
                raise RuntimeError(f"J{j}: esperaba 8 fichas y encontré {len(fichas)}.")
            # Comprueba al menos la primera ficha de cada jornada en dry-run.
            p = parse_partido(fichas[0], get_con_reintentos(session, fichas[0]).text, args.temporada)
            print(
                "   muestra:", p["fecha_hora_inicio"],
                p["local"]["nombre"], p["goles_local"], "-", p["goles_visitante"], p["visitante"]["nombre"]
            )
            total += len(fichas)
        print("DRY-RUN OK. Fichas:", total)
        return

    api = ApiIngesta()
    health = api.health()
    print("Puente IONOS OK ->", health.get("database"), health.get("db_version"))

    lote = api.iniciar_lote(
        fuente=FUENTE,
        tipo_fuente="scraping_historico",
        notas=f"Backfill Liga F {args.temporada}; jornadas={jornadas}",
    )
    print("Lote abierto:", lote)

    creados = actualizados = errores = 0
    mensajes_error: list[str] = []

    for j in jornadas:
        url = url_jornada(anio_inicio, j)
        print(f"\nJornada {j}: {url}")
        try:
            html_jornada = get_con_reintentos(session, url).text
            api.guardar_documento(
                {
                    "lote_id": lote,
                    "fuente": FUENTE,
                    "url": url,
                    "tipo_contenido": "sd_ligaf_hist_jornada",
                    "obtenido_en": ahora_sql(),
                    "contenido": html_jornada,
                }
            )
            fichas = descubrir_fichas(html_jornada)
            print("  fichas descubiertas:", len(fichas))
            if len(fichas) != 8:
                raise RuntimeError(f"Esperaba 8 fichas y encontré {len(fichas)}.")

            for ficha in fichas:
                time.sleep(max(0.0, args.pausa))
                try:
                    html = get_con_reintentos(session, ficha).text
                    payload = parse_partido(ficha, html, args.temporada)
                    if int(payload["jornada_numero"]) != j:
                        raise RuntimeError(
                            f"La ficha dice J{payload['jornada_numero']} y se esperaba J{j}."
                        )
                    payload["lote_id"] = lote
                    res = api.guardar_partido(payload)
                    if res.get("accion") == "creado":
                        creados += 1
                    else:
                        actualizados += 1
                    print(
                        f"  {payload['local']['nombre']} {payload['goles_local']}-"
                        f"{payload['goles_visitante']} {payload['visitante']['nombre']} -> "
                        f"{res.get('accion')} partido_id={res.get('partido_id')}"
                    )
                except Exception as exc:
                    errores += 1
                    mensajes_error.append(f"J{j} {ficha}: {exc}")
                    print("  ERROR partido:", exc)
        except Exception as exc:
            errores += 1
            mensajes_error.append(f"J{j}: {exc}")
            print("  ERROR jornada:", exc)

    estado = "completado" if errores == 0 else ("parcial" if creados or actualizados else "error")
    notas = f"creados={creados}; actualizados={actualizados}; errores={errores}"
    if mensajes_error:
        notas += " | " + " | ".join(mensajes_error[:3])
    api.finalizar_lote(lote, estado=estado, notas=notas)

    print(
        f"\nResumen Liga F {args.temporada}: "
        f"creados={creados}, actualizados={actualizados}, errores={errores}"
    )
    if errores:
        raise RuntimeError(mensajes_error[0])


if __name__ == "__main__":
    main()
