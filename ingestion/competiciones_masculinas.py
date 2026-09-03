#!/usr/bin/env python3
"""
Backfill de competiciones complementarias MASCULINAS para Quiniela 1X2.

Se descargan calendarios por EQUIPO ya existente en la BD. Por diseño:
- NO se importan todos los equipos de Copa/Europa.
- NO se crea ningún rival externo en nucleo_equipos.
- Si el rival también es un equipo seguido, se vinculan ambos IDs.
- Si el rival es externo, su nombre/ID Transfermarkt queda como texto.

Temporadas soportadas:
2022-23, 2023-24, 2024-25, 2025-26, 2026-27.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta


FUENTE = "transfermarkt.com"
BASE = "https://www.transfermarkt.com"

TEMPORADAS = ("2022-23", "2023-24", "2024-25", "2025-26", "2026-27")

COMPETICIONES = {
    "copa": "Copa del Rey",
    "supercopa": "Supercopa de España",
    "champions": "UEFA Champions League",
    "europa": "UEFA Europa League",
    "conference": "UEFA Conference League",
}

HEADER_MAP = {
    "copa del rey": ("Copa del Rey", False),
    "supercopa": ("Supercopa de España", False),
    "spanish super cup": ("Supercopa de España", False),

    "uefa champions league": ("UEFA Champions League", False),
    "champions league": ("UEFA Champions League", False),
    "uefa champions league qualifying": ("UEFA Champions League", True),
    "champions league qualifying": ("UEFA Champions League", True),

    "uefa europa league": ("UEFA Europa League", False),
    "europa league": ("UEFA Europa League", False),
    "uefa europa league qualifying": ("UEFA Europa League", True),
    "europa league qualifying": ("UEFA Europa League", True),

    "uefa conference league": ("UEFA Conference League", False),
    "conference league": ("UEFA Conference League", False),
    "uefa europa conference league": ("UEFA Conference League", False),
    "europa conference league": ("UEFA Conference League", False),
    "uefa conference league qualifying": ("UEFA Conference League", True),
    "conference league qualifying": ("UEFA Conference League", True),
    "uefa europa conference league qualifying": ("UEFA Conference League", True),
    "europa conference league qualifying": ("UEFA Conference League", True),
}

# Solo para mejorar la búsqueda de Transfermarkt; no cambia el nombre canónico BD.
BUSQUEDA_ALIAS = {
    "r racing club": "Racing Santander",
    "real sporting": "Sporting Gijon",
    "celta": "Celta Vigo",
    "rcd espanyol de barcelona": "Espanyol Barcelona",
    "albacete bp": "Albacete Balompie",
    "rc deportivo": "Deportivo La Coruna",
    "r sociedad b": "Real Sociedad B",
    "cultural y deportiva leonesa": "Cultural Leonesa",
    "villarreal b": "Villarreal CF B",
    "ad ceuta fc": "AD Ceuta",
    "deportivo alaves": "Deportivo Alaves",
    "atletico de madrid": "Atletico Madrid",
    "fc barcelona": "FC Barcelona",
    "real betis": "Real Betis",
    "ud las palmas": "UD Las Palmas",
    "ca osasuna": "CA Osasuna",
}

DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
TIME_12_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*([AP]M)\b", re.I)
TIME_24_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*$")
SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*:\s*(\d{1,2})(?!\d)")
TM_TEAM_RE = re.compile(r"/verein/(\d+)")
TM_MATCH_RE = re.compile(r"/spielbericht(?:/index)?/spielbericht/(\d+)")


@dataclass
class Equipo:
    equipo_id: int
    nombre: str
    transfermarkt_id: str | None = None
    transfermarkt_slug: str | None = None


@dataclass
class Partido:
    id_fuente: str
    competicion: str
    ronda: str | None
    es_clasificatoria: bool
    fecha_sql: str
    hora_confirmada: bool
    estado: str
    local_nombre: str
    visitante_nombre: str
    local_tm_id: str | None
    visitante_tm_id: str | None
    goles_local: int | None
    goles_visitante: int | None
    hubo_prorroga: bool
    hubo_penaltis: bool
    resultado_raw: str | None
    url: str


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slugify(s: str) -> str:
    return norm(s).replace(" ", "-")


def ahora_sql() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def session_transfermarkt() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
    )
    return s


def get_retry(
    session: requests.Session,
    url: str,
    *,
    timeout: int = 45,
    intentos: int = 3,
) -> requests.Response:
    ultimo: Exception | None = None
    for intento in range(1, intentos + 1):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code in {403, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            if len(r.text) < 1000:
                raise RuntimeError(
                    f"HTML sospechosamente corto ({len(r.text)} bytes)"
                )
            return r
        except Exception as exc:
            ultimo = exc
            if intento < intentos:
                espera = 2 ** (intento - 1)
                print(f"    reintento {intento}/{intentos}: {exc}; {espera}s")
                time.sleep(espera)
    raise RuntimeError(f"No pude descargar {url}: {ultimo}")


def score_nombre(esperado: str, candidato: str) -> float:
    a = norm(esperado)
    b = norm(candidato)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.93
    return SequenceMatcher(None, a, b).ratio()


def query_equipo(nombre: str) -> str:
    return BUSQUEDA_ALIAS.get(norm(nombre), nombre)


def resolver_transfermarkt(
    session: requests.Session,
    equipo: Equipo,
) -> tuple[str, str]:
    """
    Resuelve una vez el ID de Transfermarkt y devuelve (id, slug).
    Se exige una coincidencia suficientemente buena para no mezclar clubes.
    """
    buscado = query_equipo(equipo.nombre)
    url = (
        f"{BASE}/schnellsuche/ergebnis/schnellsuche?"
        f"query={quote_plus(buscado)}"
    )
    r = get_retry(session, url)
    soup = BeautifulSoup(r.text, "html.parser")

    candidatos: dict[str, tuple[float, str, str]] = {}

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = TM_TEAM_RE.search(href)
        if not m:
            continue
        tm_id = m.group(1)
        nombre = " ".join(a.stripped_strings).strip()
        if not nombre:
            title = a.get("title")
            nombre = str(title).strip() if title else ""
        if not nombre:
            continue

        # Penalizar juveniles/reservas si el nombre esperado no los pide.
        n = norm(nombre)
        penal = 0.0
        if any(x in n for x in (" u19", " u18", " youth", " ii", " b team")):
            if not any(x in norm(buscado) for x in (" u19", " u18", " youth", " b")):
                penal = 0.25

        score = max(
            score_nombre(equipo.nombre, nombre),
            score_nombre(buscado, nombre),
        ) - penal

        slug = href.strip("/").split("/")[0] or slugify(nombre)
        previo = candidatos.get(tm_id)
        if previo is None or score > previo[0]:
            candidatos[tm_id] = (score, nombre, slug)

    if not candidatos:
        raise RuntimeError(
            f"Transfermarkt no devolvió candidatos para '{equipo.nombre}'."
        )

    tm_id, (score, nombre_tm, slug) = max(
        candidatos.items(),
        key=lambda kv: kv[1][0],
    )

    if score < 0.58:
        top = sorted(
            [(i, *v) for i, v in candidatos.items()],
            key=lambda x: x[1],
            reverse=True,
        )[:5]
        raise RuntimeError(
            f"Resolución insegura para '{equipo.nombre}'. "
            f"Mejor candidato='{nombre_tm}', score={score:.2f}, "
            f"candidatos={top}"
        )

    print(
        f"    Transfermarkt: {equipo.nombre} -> {nombre_tm} "
        f"(id={tm_id}, score={score:.2f})"
    )
    return tm_id, slug


def url_calendario(equipo: Equipo, temporada: str) -> str:
    anio = temporada.split("-")[0]
    slug = equipo.transfermarkt_slug or slugify(equipo.nombre) or "club"
    return (
        f"{BASE}/{slug}/spielplan/verein/{equipo.transfermarkt_id}"
        f"/saison_id/{anio}"
    )


def encabezado_competicion(table) -> tuple[str, bool] | None:
    # Transfermarkt agrupa el calendario en bloques encabezados por competición.
    candidatos = []

    parent = table.parent
    for _ in range(5):
        if parent is None:
            break
        h = parent.find_previous(
            ["h2", "h3", "div"],
            class_=re.compile(r"(content-box-headline|box-headline)", re.I),
        )
        if h:
            candidatos.append(h.get_text(" ", strip=True))
        parent = getattr(parent, "parent", None)

    for h in table.find_all_previous(["h2", "h3", "div"], limit=30):
        cls = " ".join(h.get("class", []))
        if h.name in {"h2", "h3"} or "headline" in cls:
            candidatos.append(h.get_text(" ", strip=True))

    for texto in candidatos:
        n = norm(texto)
        for clave, val in HEADER_MAP.items():
            if n == clave or n.startswith(clave + " "):
                return val
    return None


def extraer_tm_id_anchor(anchor) -> str | None:
    if anchor is None:
        return None
    m = TM_TEAM_RE.search(anchor.get("href", ""))
    return m.group(1) if m else None


def parse_fecha_hora(date_text: str, time_text: str) -> tuple[str, bool]:
    m = DATE_RE.search(date_text)
    if not m:
        raise RuntimeError(f"Fecha Transfermarkt no reconocida: {date_text!r}")
    fecha_token = m.group(1)

    fecha = None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            fecha = datetime.strptime(fecha_token, fmt)
            break
        except ValueError:
            pass
    if fecha is None:
        raise RuntimeError(f"Fecha inválida: {fecha_token!r}")

    t = (time_text or "").strip()
    hora_confirmada = True
    hh = mm = None

    m12 = TIME_12_RE.search(t)
    if m12:
        parsed = datetime.strptime(
            f"{m12.group(1)} {m12.group(2).upper()}",
            "%I:%M %p",
        )
        hh, mm = parsed.hour, parsed.minute
    else:
        m24 = TIME_24_RE.match(t)
        if m24:
            parsed = datetime.strptime(m24.group(1), "%H:%M")
            hh, mm = parsed.hour, parsed.minute

    if hh is None:
        # Para fechas futuras Transfermarkt a veces publica "Unknown".
        # Se conserva el día sin fingir precisión: 12:00 + hora_confirmada=0.
        hh, mm = 12, 0
        hora_confirmada = False

    dt = fecha.replace(hour=hh, minute=mm, second=0)
    return dt.strftime("%Y-%m-%d %H:%M:%S"), hora_confirmada


def encontrar_oponente(row, propio_tm_id: str) -> tuple[str, str | None]:
    candidatos = []
    for a in row.find_all("a", href=True):
        tm_id = extraer_tm_id_anchor(a)
        if not tm_id or tm_id == propio_tm_id:
            continue
        nombre = " ".join(a.stripped_strings).strip()
        if not nombre:
            nombre = (a.get("title") or "").strip()
        if nombre:
            candidatos.append((nombre, tm_id))
    if candidatos:
        # En la tabla de calendario normalmente hay un único enlace de club.
        return candidatos[-1]

    # Fallback textual: buscar una celda "hauptlink" que no sea resultado.
    for td in row.find_all("td"):
        txt = td.get_text(" ", strip=True)
        if (
            txt
            and not DATE_RE.search(txt)
            and not SCORE_RE.search(txt)
            and txt not in {"H", "A", "N", "-"}
            and len(txt) <= 150
        ):
            cls = " ".join(td.get("class", []))
            if "hauptlink" in cls:
                return txt, None

    raise RuntimeError("No pude identificar al rival en una fila.")


def parse_table(
    table,
    *,
    equipo: Equipo,
    temporada: str,
    calendario_url: str,
    seleccion: set[str],
) -> list[Partido]:
    comp = encabezado_competicion(table)
    if comp is None:
        return []

    competicion, es_clasificatoria = comp
    if competicion not in seleccion:
        return []

    out: list[Partido] = []

    for row in table.find_all("tr"):
        tds = row.find_all("td")
        if not tds:
            continue

        texts = [td.get_text(" ", strip=True) for td in tds]

        date_idx = next(
            (i for i, x in enumerate(texts) if DATE_RE.search(x)),
            None,
        )
        if date_idx is None:
            continue

        # Venue H/A suele ir detrás de fecha/hora.
        venue_idx = next(
            (
                i
                for i, x in enumerate(texts)
                if i > date_idx and x.strip().upper() in {"H", "A", "N"}
            ),
            None,
        )
        if venue_idx is None:
            raise RuntimeError(
                f"Fila con fecha pero sin Venue H/A/N: {texts}"
            )

        venue = texts[venue_idx].strip().upper()
        if venue == "N":
            # Transfermarkt suele asignar H/A nominal incluso en sede neutral.
            # Si apareciera N no adivinamos el lado.
            raise RuntimeError(
                f"Venue neutral N no resoluble automáticamente: {texts}"
            )

        time_text = ""
        if date_idx + 1 < len(texts):
            time_text = texts[date_idx + 1]

        fecha_sql, hora_confirmada = parse_fecha_hora(
            texts[date_idx],
            time_text,
        )

        rival_nombre, rival_tm_id = encontrar_oponente(
            row,
            equipo.transfermarkt_id or "",
        )

        resultado_raw = None
        goles_a = goles_b = None
        result_candidates = []
        for i, txt in enumerate(texts):
            if i <= venue_idx:
                continue
            if SCORE_RE.search(txt) or "-:-" in txt:
                result_candidates.append(txt)

        if result_candidates:
            resultado_raw = result_candidates[-1].strip()
        else:
            # Algunas versiones ponen el resultado solo dentro del enlace.
            for a in row.find_all("a", href=True):
                if TM_MATCH_RE.search(a.get("href", "")):
                    txt = a.get_text(" ", strip=True)
                    if txt:
                        resultado_raw = txt
                        break

        if resultado_raw:
            sm = SCORE_RE.search(resultado_raw)
            if sm:
                goles_a, goles_b = int(sm.group(1)), int(sm.group(2))

        estado = (
            "FINALIZADO"
            if goles_a is not None and goles_b is not None
            else "PROGRAMADO"
        )

        hubo_prorroga = bool(
            resultado_raw
            and re.search(r"\b(AET|ET)\b|extra time", resultado_raw, re.I)
        )
        hubo_penaltis = bool(
            resultado_raw
            and re.search(r"\bPEN\b|pens|penalt", resultado_raw, re.I)
        )

        ronda = texts[0].strip() if texts else None
        if ronda == "":
            ronda = None

        match_id = None
        match_url = calendario_url
        for a in row.find_all("a", href=True):
            href = a.get("href", "")
            mm = TM_MATCH_RE.search(href)
            if mm:
                match_id = mm.group(1)
                match_url = (
                    href if href.startswith("http")
                    else BASE + href
                )
                break

        if venue == "H":
            local_nombre = equipo.nombre
            visitante_nombre = rival_nombre
            local_tm_id = equipo.transfermarkt_id
            visitante_tm_id = rival_tm_id
            goles_local, goles_visitante = goles_a, goles_b
        else:
            local_nombre = rival_nombre
            visitante_nombre = equipo.nombre
            local_tm_id = rival_tm_id
            visitante_tm_id = equipo.transfermarkt_id
            goles_local, goles_visitante = goles_a, goles_b

        if not match_id:
            semilla = "|".join(
                [
                    temporada,
                    competicion,
                    fecha_sql[:10],
                    norm(local_nombre),
                    norm(visitante_nombre),
                ]
            )
            match_id = "hash-" + hashlib.sha1(
                semilla.encode("utf-8")
            ).hexdigest()[:24]

        out.append(
            Partido(
                id_fuente=match_id,
                competicion=competicion,
                ronda=ronda,
                es_clasificatoria=es_clasificatoria,
                fecha_sql=fecha_sql,
                hora_confirmada=hora_confirmada,
                estado=estado,
                local_nombre=local_nombre,
                visitante_nombre=visitante_nombre,
                local_tm_id=local_tm_id,
                visitante_tm_id=visitante_tm_id,
                goles_local=goles_local,
                goles_visitante=goles_visitante,
                hubo_prorroga=hubo_prorroga,
                hubo_penaltis=hubo_penaltis,
                resultado_raw=resultado_raw,
                url=match_url,
            )
        )

    return out


def parse_calendario(
    html: str,
    *,
    equipo: Equipo,
    temporada: str,
    calendario_url: str,
    seleccion: set[str],
) -> list[Partido]:
    soup = BeautifulSoup(html, "html.parser")
    tablas = soup.select("table.items")
    if not tablas:
        # Fallback por si cambia la clase pero mantiene tablas.
        tablas = soup.find_all("table")

    encontrados: list[Partido] = []
    for table in tablas:
        encontrados.extend(
            parse_table(
                table,
                equipo=equipo,
                temporada=temporada,
                calendario_url=calendario_url,
                seleccion=seleccion,
            )
        )
    return encontrados


def merge_partido(a: Partido, b: Partido) -> Partido:
    """
    El mismo partido puede aparecer en el calendario de ambos clubes seguidos.
    Se conserva la versión más completa.
    """
    if a.id_fuente != b.id_fuente:
        raise ValueError("No se pueden fusionar partidos distintos.")

    def pick(x, y):
        return x if x not in (None, "") else y

    return Partido(
        id_fuente=a.id_fuente,
        competicion=a.competicion,
        ronda=pick(a.ronda, b.ronda),
        es_clasificatoria=a.es_clasificatoria or b.es_clasificatoria,
        fecha_sql=(
            b.fecha_sql
            if b.hora_confirmada and not a.hora_confirmada
            else a.fecha_sql
        ),
        hora_confirmada=a.hora_confirmada or b.hora_confirmada,
        estado=(
            "FINALIZADO"
            if "FINALIZADO" in {a.estado, b.estado}
            else "PROGRAMADO"
        ),
        local_nombre=pick(a.local_nombre, b.local_nombre),
        visitante_nombre=pick(a.visitante_nombre, b.visitante_nombre),
        local_tm_id=pick(a.local_tm_id, b.local_tm_id),
        visitante_tm_id=pick(a.visitante_tm_id, b.visitante_tm_id),
        goles_local=pick(a.goles_local, b.goles_local),
        goles_visitante=pick(a.goles_visitante, b.goles_visitante),
        hubo_prorroga=a.hubo_prorroga or b.hubo_prorroga,
        hubo_penaltis=a.hubo_penaltis or b.hubo_penaltis,
        resultado_raw=pick(a.resultado_raw, b.resultado_raw),
        url=pick(a.url, b.url),
    )


def elegir_competiciones(arg: str) -> set[str]:
    if arg == "todas":
        return set(COMPETICIONES.values())
    return {COMPETICIONES[arg]}


def cargar_contexto(api: ApiIngesta, temporada: str) -> list[Equipo]:
    data = api._request_json(
        "GET",
        "contexto_equipos_complementarios.php",
        params={"temporada": temporada},
        timeout=45,
    )
    items = data.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError("Contexto de equipos con formato inválido.")
    return [
        Equipo(
            equipo_id=int(x["equipo_id"]),
            nombre=str(x["nombre_canonico"]),
            transfermarkt_id=(
                str(x["transfermarkt_id"])
                if x.get("transfermarkt_id")
                else None
            ),
        )
        for x in items
    ]


def guardar_mapping(api: ApiIngesta, equipo: Equipo) -> None:
    api._request_json(
        "POST",
        "guardar_id_transfermarkt_equipo.php",
        json={
            "equipo_id": equipo.equipo_id,
            "transfermarkt_id": equipo.transfermarkt_id,
        },
        timeout=30,
    )


def payload_partido(
    p: Partido,
    *,
    temporada: str,
    lote: int,
    tm_to_db: dict[str, int],
) -> dict:
    local_db = (
        tm_to_db.get(p.local_tm_id)
        if p.local_tm_id is not None
        else None
    )
    visitante_db = (
        tm_to_db.get(p.visitante_tm_id)
        if p.visitante_tm_id is not None
        else None
    )

    if local_db is None and visitante_db is None:
        raise RuntimeError(
            f"{p.local_nombre}-{p.visitante_nombre}: "
            "ningún lado pertenece al universo seguido."
        )

    return {
        "lote_id": lote,
        "id_partido_fuente": p.id_fuente,
        "temporada": temporada,
        "competicion": p.competicion,
        "ronda": p.ronda,
        "es_clasificatoria": p.es_clasificatoria,
        "fecha_hora_inicio": p.fecha_sql,
        "hora_confirmada": p.hora_confirmada,
        "estado": p.estado,
        "equipo_local_id": local_db,
        "equipo_visitante_id": visitante_db,
        "local_nombre": p.local_nombre,
        "visitante_nombre": p.visitante_nombre,
        "local_id_fuente": p.local_tm_id,
        "visitante_id_fuente": p.visitante_tm_id,
        "goles_local": p.goles_local,
        "goles_visitante": p.goles_visitante,
        "hubo_prorroga": p.hubo_prorroga,
        "hubo_penaltis": p.hubo_penaltis,
        "resultado_raw": p.resultado_raw,
        "url": p.url,
        "obtenido_en": ahora_sql(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporada", required=True, choices=TEMPORADAS)
    parser.add_argument(
        "--competicion",
        default="todas",
        choices=("todas", *COMPETICIONES.keys()),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pausa", type=float, default=1.5)
    parser.add_argument(
        "--limite-equipos",
        type=int,
        default=0,
        help="Solo para depuración. 0 = todos.",
    )
    args = parser.parse_args()

    seleccion = elegir_competiciones(args.competicion)

    api = ApiIngesta()
    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    equipos = cargar_contexto(api, args.temporada)
    if args.limite_equipos > 0:
        equipos = equipos[: args.limite_equipos]

    if not equipos:
        raise RuntimeError(
            f"No hay equipos masculinos de LaLiga/Segunda "
            f"cargados para {args.temporada}."
        )

    print(
        f"Temporada {args.temporada}: {len(equipos)} equipos ya existentes."
    )
    print("Competencias:", ", ".join(sorted(seleccion)))

    tm = session_transfermarkt()

    # ---------------------------------------------------------------
    # Fase A: resolver IDs Transfermarkt de TODOS los equipos seguidos.
    # Así, al encontrar Real Madrid-Barcelona, podemos enlazar ambos IDs
    # sin crear a ningún rival externo.
    # ---------------------------------------------------------------
    errores_mapping: list[str] = []

    for idx, e in enumerate(equipos, start=1):
        print(f"[MAP {idx}/{len(equipos)}] {e.nombre}")
        if e.transfermarkt_id:
            print(f"    id guardado: {e.transfermarkt_id}")
            continue
        try:
            tm_id, slug = resolver_transfermarkt(tm, e)
            e.transfermarkt_id = tm_id
            e.transfermarkt_slug = slug
            if not args.dry_run:
                guardar_mapping(api, e)
            time.sleep(max(0.0, args.pausa))
        except Exception as exc:
            errores_mapping.append(f"{e.nombre}: {exc}")
            print("    ERROR mapping:", exc)

    if errores_mapping:
        print("\nERRORES DE MAPPING:")
        for x in errores_mapping:
            print(" -", x)
        raise RuntimeError(
            f"No continúo: {len(errores_mapping)} equipo(s) "
            "no pudieron reconciliarse con Transfermarkt."
        )

    tm_to_db = {
        str(e.transfermarkt_id): e.equipo_id
        for e in equipos
        if e.transfermarkt_id
    }

    lote = None
    if not args.dry_run:
        lote = api.iniciar_lote(
            fuente=FUENTE,
            tipo_fuente="calendario_complementario",
            notas=(
                f"Competiciones masculinas {args.temporada}; "
                f"seleccion={args.competicion}; "
                "solo equipos ya existentes en LaLiga/Segunda"
            ),
        )
        print("Lote abierto:", lote)

    partidos_unicos: dict[str, Partido] = {}
    paginas_ok = 0
    errores_paginas: list[str] = []

    # ---------------------------------------------------------------
    # Fase B: una página de calendario por equipo, filtrada a las 5
    # competencias solicitadas.
    # ---------------------------------------------------------------
    for idx, e in enumerate(equipos, start=1):
        if not e.transfermarkt_id:
            continue

        url = url_calendario(e, args.temporada)
        print(f"\n[{idx}/{len(equipos)}] {e.nombre}: {url}")

        try:
            r = get_retry(tm, url)
            html = r.text
            paginas_ok += 1

            if not args.dry_run and lote is not None:
                api.guardar_documento(
                    {
                        "lote_id": lote,
                        "fuente": FUENTE,
                        "url": r.url,
                        "tipo_contenido": "tm_sched",
                        "obtenido_en": ahora_sql(),
                        "contenido": html,
                    }
                )

            partidos = parse_calendario(
                html,
                equipo=e,
                temporada=args.temporada,
                calendario_url=r.url,
                seleccion=seleccion,
            )
            print(f"    partidos complementarios encontrados: {len(partidos)}")

            for p in partidos:
                if p.id_fuente in partidos_unicos:
                    partidos_unicos[p.id_fuente] = merge_partido(
                        partidos_unicos[p.id_fuente],
                        p,
                    )
                else:
                    partidos_unicos[p.id_fuente] = p

            time.sleep(max(0.0, args.pausa))

        except Exception as exc:
            errores_paginas.append(f"{e.nombre}: {exc}")
            print("    ERROR página/parser:", exc)

    if errores_paginas:
        estado_lote = "error"
    else:
        estado_lote = "completado"

    print(
        f"\nPáginas OK={paginas_ok}/{len(equipos)} | "
        f"partidos únicos={len(partidos_unicos)}"
    )

    # Dry-run: no escribir.
    if args.dry_run:
        por_comp: dict[str, int] = {}
        for p in partidos_unicos.values():
            por_comp[p.competicion] = por_comp.get(p.competicion, 0) + 1

        print("\nDRY-RUN. Conteo por competición:")
        for k in sorted(por_comp):
            print(f"  {k}: {por_comp[k]}")

        if errores_paginas:
            print("\nERRORES:")
            for x in errores_paginas:
                print(" -", x)
            raise RuntimeError(
                f"Dry-run con {len(errores_paginas)} errores."
            )

        print("DRY-RUN OK. No se escribió nada en IONOS.")
        return

    assert lote is not None

    creados = actualizados = errores_guardado = 0
    mensajes_guardado: list[str] = []

    for p in sorted(
        partidos_unicos.values(),
        key=lambda x: (x.fecha_sql, x.competicion, x.id_fuente),
    ):
        try:
            payload = payload_partido(
                p,
                temporada=args.temporada,
                lote=lote,
                tm_to_db=tm_to_db,
            )
            res = api._request_json(
                "POST",
                "guardar_partido_complementario.php",
                json=payload,
                timeout=60,
            )
            if res.get("accion") == "creado":
                creados += 1
            else:
                actualizados += 1

            gl = "?" if p.goles_local is None else p.goles_local
            gv = "?" if p.goles_visitante is None else p.goles_visitante
            print(
                f"{p.fecha_sql[:10]} [{p.competicion}] "
                f"{p.local_nombre} {gl}-{gv} {p.visitante_nombre} "
                f"-> {res.get('accion')}"
            )
        except Exception as exc:
            errores_guardado += 1
            mensajes_guardado.append(
                f"{p.id_fuente} {p.local_nombre}-{p.visitante_nombre}: {exc}"
            )
            print("ERROR guardando partido:", exc)

    total_errores = len(errores_paginas) + errores_guardado
    estado_lote = "completado" if total_errores == 0 else "error"

    api.finalizar_lote(
        lote,
        estado=estado_lote,
        notas=(
            f"paginas_ok={paginas_ok}/{len(equipos)}; "
            f"partidos_unicos={len(partidos_unicos)}; "
            f"creados={creados}; actualizados={actualizados}; "
            f"errores={total_errores}"
        ),
    )

    print(
        f"\nRESUMEN {args.temporada}: "
        f"creados={creados}, actualizados={actualizados}, "
        f"errores={total_errores}"
    )

    if errores_paginas:
        print("\nErrores de páginas/parser:")
        for x in errores_paginas:
            print(" -", x)

    if mensajes_guardado:
        print("\nErrores de guardado:")
        for x in mensajes_guardado:
            print(" -", x)

    if total_errores:
        raise RuntimeError(
            f"Backfill terminó con {total_errores} errores."
        )


if __name__ == "__main__":
    main()
