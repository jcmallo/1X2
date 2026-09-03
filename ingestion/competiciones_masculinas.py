#!/usr/bin/env python3
"""
Backfill de competiciones masculinas complementarias - v2.3.

CAMBIO CLAVE: Transfermarkt queda eliminado del proceso. No se usa HTML con
JavaScript/anti-bot ni búsqueda de IDs externos.

Cobertura validada: 2022-23, 2023-24, 2024-25 y 2025-26.
- Copa del Rey: OpenFootball (raw.githubusercontent.com)
- Champions: OpenFootball
- Europa League: OpenFootball
- Conference League: OpenFootball
- Supercopa: catálogo pequeño de partidos verificados

Un partido solo se guarda si al menos uno de los clubes se resuelve contra los
equipos masculinos YA existentes en LaLiga/Segunda de esa temporada. Nunca se
crea un rival externo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta

TEMPORADAS_OK = ("2022-23", "2023-24", "2024-25", "2025-26")
COMPETICIONES = {
    "copa": "Copa del Rey",
    "supercopa": "Supercopa de España",
    "champions": "UEFA Champions League",
    "europa": "UEFA Europa League",
    "conference": "UEFA Conference League",
}
URLS_OPENFOOTBALL = {
    "copa": "https://raw.githubusercontent.com/openfootball/espana/master/{temporada}/cup.txt",
    "champions": "https://raw.githubusercontent.com/openfootball/champions-league/master/{temporada}/cl.txt",
    "europa": "https://raw.githubusercontent.com/openfootball/champions-league/master/{temporada}/el.txt",
    "conference": "https://raw.githubusercontent.com/openfootball/champions-league/master/{temporada}/conf.txt",
}
URLS_2025_26 = {
    "copa": "https://en.wikipedia.org/wiki/2025%E2%80%9326_Copa_del_Rey",
    "champions": "https://fixturedownload.com/feed/json/champions-league-2025",
    "europa": "https://fixturedownload.com/feed/json/europa-league-2025",
    "conference": "https://fixturedownload.com/feed/json/conference-league-2025",
    "conference_qualifying": "https://raw.githubusercontent.com/openfootball/champions-league/master/2025-26/confq.txt",
}
FIXTURE_MIN_TOTAL = {
    "champions": 180,   # 189 oficiales en el feed 2025-26
    "europa": 180,      # 189 oficiales en el feed 2025-26
    "conference": 145,  # 153 oficiales en el feed 2025-26
}
WIKI_COPA_MIN_MATCHBOXES = 120
MESES = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
ALIAS_A_CANONICO = {
    "albacete": "albacete bp",
    "albacete balompie": "albacete bp",
    "athletic bilbao": "athletic club",
    "atletico madrid": "atletico de madrid",
    "cd alaves": "deportivo alaves",
    "deportivo la coruna": "rc deportivo",
    "espanyol barcelona": "rcd espanyol de barcelona",
    "rcd espanyol": "rcd espanyol de barcelona",
    "racing santander": "r racing club",
    "real racing club": "r racing club",
    "rc celta": "celta",
    "celta vigo": "celta",
    "sporting gijon": "real sporting",
    "real valladolid": "real valladolid cf",
    "real madrid cf": "real madrid",
    "club atletico de madrid": "atletico de madrid",
    "real sociedad de futbol": "real sociedad",
    "villarreal cf b": "villarreal b",
    "villarreal b": "villarreal b",
    # Variantes usadas por Wikipedia / FixtureDownload en 2025-26.
    "alaves": "deportivo alaves",
    "barcelona": "fc barcelona",
    "burgos": "burgos cf",
    "cadiz": "cadiz cf",
    "castellon": "cd castellon",
    "cartagena": "fc cartagena",
    "ceuta": "ad ceuta fc",
    "cordoba": "cordoba cf",
    "cultural leonesa": "cultural y deportiva leonesa",
    "eibar": "sd eibar",
    "elche": "elche cf",
    "espanyol": "rcd espanyol de barcelona",
    "getafe": "getafe cf",
    "girona": "girona fc",
    "granada": "granada cf",
    "huesca": "sd huesca",
    "las palmas": "ud las palmas",
    "leganes": "cd leganes",
    "levante": "levante ud",
    "malaga": "malaga cf",
    "mallorca": "rcd mallorca",
    "mirandes": "cd mirandes",
    "osasuna": "ca osasuna",
    "oviedo": "real oviedo",
    "racing de santander": "r racing club",
    "real racing club de santander": "r racing club",
    "sevilla": "sevilla fc",
    "sporting de gijon": "real sporting",
    "tenerife": "cd tenerife",
    "valencia": "valencia cf",
    "valladolid": "real valladolid cf",
    "villarreal": "villarreal cf",
    "zaragoza": "real zaragoza",
    "almeria": "ud almeria",
    "atleti": "atletico de madrid",
}
SUPERCOPA = {
    "2022-23": [
        ("2023-01-11 20:00:00", "Semifinal", "Real Madrid", "Valencia CF", 1, 1, True, True),
        ("2023-01-12 20:00:00", "Semifinal", "Real Betis", "FC Barcelona", 2, 2, True, True),
        ("2023-01-15 20:00:00", "Final", "Real Madrid", "FC Barcelona", 1, 3, False, False),
    ],
    "2023-24": [
        ("2024-01-10 20:00:00", "Semifinal", "Real Madrid", "Atlético de Madrid", 5, 3, True, False),
        ("2024-01-11 20:00:00", "Semifinal", "FC Barcelona", "CA Osasuna", 2, 0, False, False),
        ("2024-01-14 20:00:00", "Final", "Real Madrid", "FC Barcelona", 4, 1, False, False),
    ],
    "2024-25": [
        ("2025-01-08 20:00:00", "Semifinal", "Athletic Club", "FC Barcelona", 0, 2, False, False),
        ("2025-01-09 20:00:00", "Semifinal", "Real Madrid", "RCD Mallorca", 3, 0, False, False),
        ("2025-01-12 20:00:00", "Final", "Real Madrid", "FC Barcelona", 2, 5, False, False),
    ],
    "2025-26": [
        ("2026-01-07 20:00:00", "Semifinal", "FC Barcelona", "Athletic Club", 5, 0, False, False),
        ("2026-01-08 20:00:00", "Semifinal", "Atlético de Madrid", "Real Madrid", 1, 2, False, False),
        ("2026-01-11 20:00:00", "Final", "FC Barcelona", "Real Madrid", 3, 2, False, False),
    ],
}
DATE_LINE_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2})(?:\s+(\d{4}))?$"
)
TIME_PREFIX_RE = re.compile(r"^(\d{1,2}:\d{2})\s+(.*)$")
COUNTRY_SUFFIX_RE = re.compile(r"\s+\([A-Z]{3}\)\s*$")
SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)")

@dataclass(frozen=True)
class Equipo:
    equipo_id: int
    nombre: str

@dataclass
class Partido:
    fuente: str
    id_fuente: str
    competicion: str
    ronda: str | None
    fecha_sql: str
    local: str
    visitante: str
    goles_local: int | None
    goles_visitante: int | None
    hubo_prorroga: bool
    hubo_penaltis: bool
    resultado_raw: str | None
    url: str
    equipo_local_id: int | None = None
    equipo_visitante_id: int | None = None
    es_clasificatoria: bool = False

def norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower().replace("&", " and ")
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()

def quitar_pais(nombre: str) -> str:
    return COUNTRY_SUFFIX_RE.sub("", nombre).strip()

def limpiar_nombre_fuente(nombre: str) -> str:
    nombre = quitar_pais(nombre)
    # OpenFootball puede añadir anotaciones al final del club.
    nombre = re.sub(r"\s+\[[^\]]+\]\s*$", "", nombre).strip()
    return nombre

def canonical_key(nombre: str) -> str:
    n = norm(limpiar_nombre_fuente(nombre))
    return ALIAS_A_CANONICO.get(n, n)

def indice_equipos(equipos: Iterable[Equipo]) -> dict[str, Equipo]:
    idx: dict[str, Equipo] = {}
    for e in equipos:
        k = canonical_key(e.nombre)
        if k in idx and idx[k].equipo_id != e.equipo_id:
            raise RuntimeError(f"Colisión de equipos al normalizar: {e.nombre}")
        idx[k] = e
    return idx

def resolver_equipo(nombre_fuente: str, idx: dict[str, Equipo]) -> Equipo | None:
    return idx.get(canonical_key(nombre_fuente))

def descargar_texto(url: str, timeout: int = 45) -> str:
    headers = {
        "User-Agent": "quiniela-1x2-github-actions/2.0",
        "Accept": "text/plain,*/*;q=0.8",
    }
    ultimo = None
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            texto = r.text
            if len(texto) < 200 or not texto.lstrip().startswith("="):
                raise RuntimeError(
                    f"respuesta inesperada: {len(texto)} bytes; inicio={texto[:80]!r}"
                )
            return texto
        except Exception as exc:
            ultimo = exc
    raise RuntimeError(f"No pude descargar fuente estática {url}: {ultimo}")


def descargar_json(url: str, timeout: int = 45) -> tuple[list[dict], str]:
    headers = {
        "User-Agent": "quiniela-1x2-github-actions/2.3",
        "Accept": "application/json,text/plain,*/*;q=0.8",
    }
    ultimo = None
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError(f"JSON no es una lista: {type(data).__name__}")
            return data, r.text
        except Exception as exc:
            ultimo = exc
    raise RuntimeError(f"No pude descargar JSON {url}: {ultimo}")


def descargar_html(url: str, timeout: int = 60) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/140.0 Safari/537.36 quiniela-1x2/2.3"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    ultimo = None
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            html = r.text
            if len(html) < 100_000 or "Copa del Rey" not in html:
                raise RuntimeError(
                    f"HTML Wikipedia inesperado: {len(html)} bytes; "
                    f"inicio={html[:100]!r}"
                )
            return html
        except Exception as exc:
            ultimo = exc
    raise RuntimeError(f"No pude descargar Wikipedia {url}: {ultimo}")


def utc_a_madrid(fecha_utc: str) -> str:
    dt = datetime.strptime(fecha_utc, "%Y-%m-%d %H:%M:%SZ")
    dt = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Madrid"))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def ronda_fixture(competicion: str, numero: int) -> str:
    if competicion in {"UEFA Champions League", "UEFA Europa League"}:
        etiquetas = {
            9: "Play-off ida", 10: "Play-off vuelta",
            11: "Octavos ida", 12: "Octavos vuelta",
            13: "Cuartos ida", 14: "Cuartos vuelta",
            15: "Semifinal ida", 16: "Semifinal vuelta", 17: "Final",
        }
        return f"Fase liga J{numero}" if numero <= 8 else etiquetas.get(numero, f"Ronda {numero}")
    etiquetas = {
        7: "Play-off ida", 8: "Play-off vuelta",
        9: "Octavos ida", 10: "Octavos vuelta",
        11: "Cuartos ida", 12: "Cuartos vuelta",
        13: "Semifinal ida", 14: "Semifinal vuelta", 15: "Final",
    }
    return f"Fase liga J{numero}" if numero <= 6 else etiquetas.get(numero, f"Ronda {numero}")


def parse_fixturedownload(
    data: list[dict], *, temporada: str, competicion: str, url: str,
    idx: dict[str, Equipo], min_total: int,
) -> list[Partido]:
    if len(data) < min_total:
        raise RuntimeError(
            f"FixtureDownload devolvió solo {len(data)} partidos para {competicion}; "
            f"mínimo de seguridad={min_total}. No se escribirá nada."
        )

    requeridos = {
        "MatchNumber", "RoundNumber", "DateUtc", "HomeTeam", "AwayTeam",
        "HomeTeamScore", "AwayTeamScore",
    }
    out: list[Partido] = []
    for pos, row in enumerate(data, start=1):
        if not isinstance(row, dict) or not requeridos.issubset(row):
            raise RuntimeError(
                f"FixtureDownload cambió el esquema en fila {pos}. "
                "No se escribirá nada."
            )
        if row["HomeTeamScore"] is None or row["AwayTeamScore"] is None:
            raise RuntimeError(
                f"FixtureDownload tiene un resultado incompleto en fila {pos}. "
                "2025-26 debe estar finalizada; no se escribirá nada."
            )

        local = limpiar_nombre_fuente(str(row["HomeTeam"]).strip())
        visitante = limpiar_nombre_fuente(str(row["AwayTeam"]).strip())
        ldb = resolver_equipo(local, idx)
        vdb = resolver_equipo(visitante, idx)
        if ldb is None and vdb is None:
            continue

        num = int(row["MatchNumber"])
        rnd = int(row["RoundNumber"])
        gl = int(row["HomeTeamScore"])
        gv = int(row["AwayTeamScore"])
        out.append(Partido(
            fuente="fixturedownload",
            id_fuente=f"{temporada}-{norm(competicion)}-{num}",
            competicion=competicion,
            ronda=ronda_fixture(competicion, rnd),
            fecha_sql=utc_a_madrid(str(row["DateUtc"])),
            local=local,
            visitante=visitante,
            goles_local=gl,
            goles_visitante=gv,
            hubo_prorroga=False,
            hubo_penaltis=False,
            resultado_raw=f"{gl}-{gv}",
            url=url,
            equipo_local_id=ldb.equipo_id if ldb else None,
            equipo_visitante_id=vdb.equipo_id if vdb else None,
        ))

    if not out:
        raise RuntimeError(
            f"FixtureDownload no produjo ningún partido de nuestros equipos para "
            f"{competicion} {temporada}. No se escribirá nada."
        )
    return out


WIKI_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})\b",
    re.I,
)
WIKI_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
WIKI_SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[–—-]\s*(\d{1,2})(?!\d)")


def limpiar_equipo_wiki(nombre: str) -> str:
    nombre = re.sub(r"\[[^\]]+\]", "", nombre)
    # La Copa marca la categoría del club: Burgos (2), Atlético Tordesillas (5).
    nombre = re.sub(r"\s+\(\d+\)\s*$", "", nombre).strip()
    return limpiar_nombre_fuente(nombre)


def resultado_wiki(texto: str) -> tuple[int, int, bool, bool]:
    m = WIKI_SCORE_RE.search(texto)
    if not m:
        raise RuntimeError(f"Resultado Wikipedia no reconocido: {texto!r}")
    low = norm(texto)
    prorroga = "a e t" in low or "extra time" in low
    penaltis = "pen" in low or "pens" in low or re.search(r"\bp\b", low) is not None
    return int(m.group(1)), int(m.group(2)), prorroga, penaltis


def parse_wikipedia_copa(
    html: str, *, temporada: str, url: str, idx: dict[str, Equipo],
) -> tuple[list[Partido], int]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Partido] = []
    vistos: set[tuple[str, str, str]] = set()
    parseados_total = 0

    # footballbox suele envolver cada ficha. El fallback a table cubre cambios
    # de clase del template de MediaWiki.
    contenedores = soup.select(".footballbox, .vevent")
    if not contenedores:
        contenedores = soup.find_all("table")

    for cont in contenedores:
        texto_cont = " ".join(cont.stripped_strings)
        dm = WIKI_DATE_RE.search(texto_cont)
        tm = WIKI_TIME_RE.search(texto_cont)
        if not dm or not tm:
            continue

        score_row = None
        score_idx = None
        score_text = None
        for tr in cont.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) < 3:
                continue
            for i, cell in enumerate(cells):
                ct = cell.get_text(" ", strip=True)
                if WIKI_SCORE_RE.search(ct) and i >= 1 and i + 1 < len(cells):
                    score_row, score_idx, score_text = cells, i, ct
                    break
            if score_row is not None:
                break
        if score_row is None or score_idx is None or score_text is None:
            continue

        local = limpiar_equipo_wiki(score_row[score_idx - 1].get_text(" ", strip=True))
        visitante = limpiar_equipo_wiki(score_row[score_idx + 1].get_text(" ", strip=True))
        if not local or not visitante:
            continue

        dia, mes, anyo = dm.groups()
        fecha = datetime.strptime(f"{dia} {mes} {anyo}", "%d %B %Y")
        hh, mm = int(tm.group(1)), int(tm.group(2))
        # Wikipedia expresa explícitamente WET/WEST en Canarias. Para guardar
        # todos los partidos en Europe/Madrid, sumar una hora en esos casos.
        if re.search(r"\b(?:WET|WEST)\b", texto_cont):
            fecha = fecha.replace(hour=hh, minute=mm) + timedelta(hours=1)
        else:
            fecha = fecha.replace(hour=hh, minute=mm)
        fecha_sql = fecha.strftime("%Y-%m-%d %H:%M:%S")

        gl, gv, pro, pen = resultado_wiki(score_text)
        key = (fecha_sql, canonical_key(local), canonical_key(visitante))
        if key in vistos:
            continue
        vistos.add(key)
        parseados_total += 1

        ldb = resolver_equipo(local, idx)
        vdb = resolver_equipo(visitante, idx)
        if ldb is None and vdb is None:
            continue

        heading = cont.find_previous(["h2", "h3", "h4", "h5"])
        ronda = heading.get_text(" ", strip=True) if heading else None
        semilla = "|".join(["wikipedia", temporada, fecha_sql, norm(local), norm(visitante)])
        out.append(Partido(
            fuente="wikipedia",
            id_fuente=hashlib.sha1(semilla.encode("utf-8")).hexdigest(),
            competicion="Copa del Rey",
            ronda=ronda,
            fecha_sql=fecha_sql,
            local=local,
            visitante=visitante,
            goles_local=gl,
            goles_visitante=gv,
            hubo_prorroga=pro,
            hubo_penaltis=pen,
            resultado_raw=score_text,
            url=url,
            equipo_local_id=ldb.equipo_id if ldb else None,
            equipo_visitante_id=vdb.equipo_id if vdb else None,
        ))

    if parseados_total < WIKI_COPA_MIN_MATCHBOXES:
        raise RuntimeError(
            f"Wikipedia Copa 2025-26: solo reconocí {parseados_total} fichas de partido; "
            f"mínimo de seguridad={WIKI_COPA_MIN_MATCHBOXES}. No se escribirá nada."
        )
    if not out:
        raise RuntimeError(
            "Wikipedia Copa 2025-26 no produjo partidos de nuestros equipos. "
            "No se escribirá nada."
        )
    return out, parseados_total

def fecha_de_linea(linea: str, temporada: str) -> str | None:
    m = DATE_LINE_RE.match(linea.strip())
    if not m:
        return None
    mes_txt, dia_txt, anyo_txt = m.groups()
    mes = MESES[mes_txt]
    dia = int(dia_txt)
    if anyo_txt:
        anyo = int(anyo_txt)
    else:
        inicio = int(temporada[:4])
        anyo = inicio if mes >= 7 else inicio + 1
    return f"{anyo:04d}-{mes:02d}-{dia:02d}"

def resultado_principal(resultado: str) -> tuple[int | None, int | None, bool, bool]:
    pen = "pen." in resultado.lower()
    aet = "a.e.t." in resultado.lower()
    scores = SCORE_RE.findall(resultado)
    if not scores:
        return None, None, aet, pen
    gl, gv = scores[1] if pen and len(scores) >= 2 else scores[0]
    return int(gl), int(gv), aet, pen

def separar_copa(cuerpo: str) -> tuple[str, str, str] | None:
    """
    OpenFootball usa dos formatos distintos para Copa del Rey:

    2022-23 / 2023-24:
        LOCAL 1-0 (0-0) VISITANTE

    2024-25:
        LOCAL v VISITANTE 1-0 (0-0)

    Detectamos explícitamente el formato en vez de asumir uno solo.
    """
    # Formato nuevo: LOCAL v VISITANTE RESULTADO
    if re.search(r"\s+v\s+", cuerpo):
        return separar_uefa(cuerpo)

    # Formato antiguo: LOCAL RESULTADO VISITANTE
    matches = list(SCORE_RE.finditer(cuerpo))
    if not matches:
        return None

    inicio = matches[0].start()
    local = cuerpo[:inicio].strip()
    resto = cuerpo[inicio:].strip()

    patron = re.compile(
        r"^((?:\d{1,2}-\d{1,2}\s+pen\.\s+)?"
        r"\d{1,2}-\d{1,2}(?:\s+a\.e\.t\.)?"
        r"(?:\s+\([^)]*\))?)\s+(.+)$"
    )
    m = patron.match(resto)
    if not m:
        return None

    resultado, visitante = m.groups()
    return (
        limpiar_nombre_fuente(local),
        limpiar_nombre_fuente(visitante),
        resultado.strip(),
    )

def separar_uefa(cuerpo: str) -> tuple[str, str, str] | None:
    partes = re.split(r"\s+v\s+", cuerpo, maxsplit=1)
    if len(partes) != 2:
        return None
    local = partes[0].strip()
    der = partes[1].strip()
    matches = list(SCORE_RE.finditer(der))
    if not matches:
        return None
    inicio = matches[0].start()
    visitante = der[:inicio].strip()
    resultado = der[inicio:].strip()
    return limpiar_nombre_fuente(local), limpiar_nombre_fuente(visitante), resultado

def parse_openfootball(texto: str, *, temporada: str, competicion: str, url: str,
                       idx: dict[str, Equipo], es_clasificatoria: bool = False) -> list[Partido]:
    fecha: str | None = None
    hora: str | None = None
    ronda: str | None = None
    out: list[Partido] = []

    m_esperados = re.search(r"(?m)^# Matches\s+(\d+)\s*$", texto)
    esperados_fuente = int(m_esperados.group(1)) if m_esperados else None
    parseados_fuente = 0
    for raw in texto.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("="):
            continue
        if stripped.startswith("▪"):
            ronda = stripped.lstrip("▪").strip() or None
            continue
        f = fecha_de_linea(stripped, temporada)
        if f:
            fecha = f
            hora = None
            continue
        if fecha is None:
            continue
        cuerpo = stripped
        mt = TIME_PREFIX_RE.match(cuerpo)
        if mt:
            hora = mt.group(1)
            cuerpo = mt.group(2).strip()
        if hora is None:
            continue
        sep = separar_copa(cuerpo) if competicion == "Copa del Rey" else separar_uefa(cuerpo)
        if not sep:
            continue
        local, visitante, resultado = sep
        gl, gv, prorroga, penaltis = resultado_principal(resultado)
        if gl is None or gv is None:
            continue

        parseados_fuente += 1

        local_db = resolver_equipo(local, idx)
        visitante_db = resolver_equipo(visitante, idx)
        if local_db is None and visitante_db is None:
            continue
        fecha_sql = f"{fecha} {hora}:00"
        semilla = "|".join(["openfootball", temporada, competicion, fecha_sql, norm(local), norm(visitante)])
        out.append(Partido(
            fuente="openfootball",
            id_fuente=hashlib.sha1(semilla.encode("utf-8")).hexdigest(),
            competicion=competicion,
            ronda=ronda,
            fecha_sql=fecha_sql,
            local=local,
            visitante=visitante,
            goles_local=gl,
            goles_visitante=gv,
            hubo_prorroga=prorroga,
            hubo_penaltis=penaltis,
            resultado_raw=resultado,
            url=url,
            equipo_local_id=local_db.equipo_id if local_db else None,
            equipo_visitante_id=visitante_db.equipo_id if visitante_db else None,
            es_clasificatoria=es_clasificatoria,
        ))
    if esperados_fuente is not None:
        minimo = max(1, int(esperados_fuente * 0.90))
        if parseados_fuente < minimo:
            raise RuntimeError(
                f"Parser OpenFootball solo reconoció {parseados_fuente}/"
                f"{esperados_fuente} partidos de la fuente para "
                f"{competicion} {temporada}. No se escribirá nada."
            )

    if not out:
        raise RuntimeError(
            f"Parser OpenFootball devolvió 0 partidos relevantes para {competicion} {temporada}. "
            "No se escribirá nada."
        )
    return out

def partidos_supercopa(temporada: str, idx: dict[str, Equipo]) -> list[Partido]:
    out: list[Partido] = []
    for fecha, ronda, local, visitante, gl, gv, pro, pen in SUPERCOPA.get(temporada, []):
        ldb = resolver_equipo(local, idx)
        vdb = resolver_equipo(visitante, idx)
        if ldb is None and vdb is None:
            continue
        semilla = "|".join(["supercopa-verificada", temporada, fecha, norm(local), norm(visitante)])
        out.append(Partido(
            fuente="supercopa-verificada",
            id_fuente=hashlib.sha1(semilla.encode("utf-8")).hexdigest(),
            competicion="Supercopa de España",
            ronda=ronda,
            fecha_sql=fecha,
            local=local,
            visitante=visitante,
            goles_local=gl,
            goles_visitante=gv,
            hubo_prorroga=pro,
            hubo_penaltis=pen,
            resultado_raw=f"{gl}-{gv}",
            url="https://www.rfef.es/competiciones/supercopa-de-espana",
            equipo_local_id=ldb.equipo_id if ldb else None,
            equipo_visitante_id=vdb.equipo_id if vdb else None,
        ))
    return out


def cargar_2025_26(clave: str, idx: dict[str, Equipo]) -> tuple[list[Partido], list[tuple[str, str, str, str]]]:
    comp = COMPETICIONES[clave]
    raws: list[tuple[str, str, str, str]] = []

    if clave == "supercopa":
        return partidos_supercopa("2025-26", idx), raws

    if clave == "copa":
        url = URLS_2025_26["copa"]
        print(f"Descargando Copa del Rey: {url}")
        html = descargar_html(url)
        ps, total = parse_wikipedia_copa(html, temporada="2025-26", url=url, idx=idx)
        print(f"  Wikipedia OK: {total} fichas completas; relevantes={len(ps)}")
        snapshot = json.dumps([
            {
                "fecha": p.fecha_sql, "ronda": p.ronda,
                "local": p.local, "visitante": p.visitante,
                "goles_local": p.goles_local, "goles_visitante": p.goles_visitante,
                "prorroga": p.hubo_prorroga, "penaltis": p.hubo_penaltis,
            }
            for p in ps
        ], ensure_ascii=False)
        raws.append(("wikipedia", url, "wiki_matches", snapshot))
        return ps, raws

    url = URLS_2025_26[clave]
    print(f"Descargando {comp}: {url}")
    data, raw = descargar_json(url)
    ps = parse_fixturedownload(
        data, temporada="2025-26", competicion=comp, url=url, idx=idx,
        min_total=FIXTURE_MIN_TOTAL[clave],
    )
    print(f"  FixtureDownload OK: {len(data)} partidos fuente; relevantes={len(ps)}")
    raws.append(("fixturedownload", url, "fixture_json", raw))

    if clave == "conference":
        qurl = URLS_2025_26["conference_qualifying"]
        print(f"Descargando previa Conference: {qurl}")
        qtext = descargar_texto(qurl)
        qps = parse_openfootball(
            qtext, temporada="2025-26", competicion=comp, url=qurl, idx=idx,
            es_clasificatoria=True,
        )
        print(f"  previa Conference relevante: {len(qps)}")
        ps.extend(qps)
        raws.append(("openfootball", qurl, "openfootball_txt", qtext))

    return ps, raws

def cargar_contexto(api: ApiIngesta, temporada: str) -> list[Equipo]:
    data = api._request_json(
        "GET", "contexto_equipos_complementarios.php",
        params={"temporada": temporada}, timeout=45,
    )
    items = data.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError("Contexto de equipos con formato inválido.")
    return [Equipo(int(x["equipo_id"]), str(x["nombre_canonico"])) for x in items]

def seleccion_claves(arg: str) -> list[str]:
    return ["copa", "supercopa", "champions", "europa", "conference"] if arg == "todas" else [arg]

def ahora_sql() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--temporada", required=True,
                    choices=("2022-23", "2023-24", "2024-25", "2025-26", "2026-27"))
    ap.add_argument("--competicion", default="todas",
                    choices=("todas", "copa", "supercopa", "champions", "europa", "conference"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.temporada not in TEMPORADAS_OK:
        raise RuntimeError(
            f"La v2 está validada para {', '.join(TEMPORADAS_OK)}. "
            f"No ejecuto {args.temporada} para no introducir otra fuente sin validar."
        )
    api = ApiIngesta()
    health = api.health()
    print("Puente IONOS OK ->", health.get("database"), health.get("db_version"))
    equipos = cargar_contexto(api, args.temporada)
    if not equipos:
        raise RuntimeError("IONOS devolvió 0 equipos seguidos.")
    idx = indice_equipos(equipos)
    print(f"Temporada {args.temporada}: {len(equipos)} equipos ya existentes.")
    print("Transfermarkt: DESACTIVADO")
    partidos: list[Partido] = []
    raws: list[tuple[str, str, str, str]] = []
    for clave in seleccion_claves(args.competicion):
        if args.temporada == "2025-26":
            ps, nuevos_raws = cargar_2025_26(clave, idx)
            if clave == "supercopa":
                print(f"Supercopa: {len(ps)} partidos relevantes")
            partidos.extend(ps)
            raws.extend(nuevos_raws)
            continue

        if clave == "supercopa":
            ps = partidos_supercopa(args.temporada, idx)
            print(f"Supercopa: {len(ps)} partidos relevantes")
            partidos.extend(ps)
            continue
        url = URLS_OPENFOOTBALL[clave].format(temporada=args.temporada)
        comp = COMPETICIONES[clave]
        print(f"Descargando {comp}: {url}")
        texto = descargar_texto(url)
        cabecera = texto.splitlines()[0].strip() if texto.splitlines() else ""
        print(f"  fuente OK: {cabecera} ({len(texto)} bytes)")
        ps = parse_openfootball(texto, temporada=args.temporada, competicion=comp, url=url, idx=idx)
        print(f"  partidos de nuestros equipos: {len(ps)}")
        partidos.extend(ps)
        raws.append(("openfootball", url, "openfootball_txt", texto))
    unicos: dict[tuple[str, str], Partido] = {}
    for p in partidos:
        unicos[(p.fuente, p.id_fuente)] = p
    partidos = list(unicos.values())
    if args.dry_run:
        print("\nDRY-RUN OK: no se escribió nada en IONOS.")
        por_comp: dict[str, int] = {}
        for p in partidos:
            por_comp[p.competicion] = por_comp.get(p.competicion, 0) + 1
        for comp, n in sorted(por_comp.items()):
            print(f"  {comp}: {n}")
        print(f"TOTAL relevantes: {len(partidos)}")
        return
    lote = api.iniciar_lote(
        fuente="multi-futbol" if args.temporada == "2025-26" else "openfootball", tipo_fuente="cal_extra",
        notas=f"Competiciones masculinas v2.3 {args.temporada}; seleccion={args.competicion}; sin Transfermarkt",
    )
    print("Lote abierto:", lote)
    for fuente, url, tipo_contenido, contenido in raws:
        api.guardar_documento({
            "lote_id": lote, "fuente": fuente, "url": url,
            "tipo_contenido": tipo_contenido, "obtenido_en": ahora_sql(), "contenido": contenido,
        })
    creados = actualizados = errores = 0
    mensajes: list[str] = []
    for p in sorted(partidos, key=lambda x: (x.fecha_sql, x.competicion, x.local)):
        payload = {
            "lote_id": lote, "fuente": p.fuente, "id_partido_fuente": p.id_fuente,
            "temporada": args.temporada, "competicion": p.competicion, "ronda": p.ronda,
            "es_clasificatoria": p.es_clasificatoria, "fecha_hora_inicio": p.fecha_sql,
            "hora_confirmada": True, "estado": "FINALIZADO",
            "equipo_local_id": p.equipo_local_id, "equipo_visitante_id": p.equipo_visitante_id,
            "local_nombre": p.local, "visitante_nombre": p.visitante,
            "local_id_fuente": None, "visitante_id_fuente": None,
            "goles_local": p.goles_local, "goles_visitante": p.goles_visitante,
            "hubo_prorroga": p.hubo_prorroga, "hubo_penaltis": p.hubo_penaltis,
            "resultado_raw": p.resultado_raw, "url": p.url, "obtenido_en": ahora_sql(),
        }
        try:
            res = api._request_json("POST", "guardar_partido_complementario.php", json=payload, timeout=60)
            if res.get("accion") == "creado": creados += 1
            else: actualizados += 1
            print(f"{p.fecha_sql[:16]} [{p.competicion}] {p.local} {p.goles_local}-{p.goles_visitante} {p.visitante} -> {res.get('accion')}")
        except Exception as exc:
            errores += 1
            msg = f"{p.competicion} {p.local}-{p.visitante}: {exc}"
            mensajes.append(msg)
            print("ERROR:", msg)
    api.finalizar_lote(
        lote, estado="completado" if errores == 0 else "error",
        notas=f"v2.3; partidos={len(partidos)}; creados={creados}; actualizados={actualizados}; errores={errores}",
    )
    print(f"\nRESUMEN {args.temporada}: creados={creados}, actualizados={actualizados}, errores={errores}, total={len(partidos)}")
    if mensajes:
        for m in mensajes: print(" -", m)
        raise RuntimeError(f"Backfill terminó con {errores} errores.")

if __name__ == "__main__":
    main()
