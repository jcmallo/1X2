#!/usr/bin/env python3
"""
Competiciones complementarias femeninas - Quiniela 1X2

Carga, para 2022-23 .. 2026-27:
- Copa de la Reina
- Supercopa Femenina
- UEFA Women's Champions League
- rondas de clasificación UWCL (dentro de la misma competición)

Diseño aprendido del backfill masculino:
- NO hay bloqueos artificiales por temporada.
- El mismo archivo sirve para las cinco temporadas.
- Si la temporada actual todavía no tiene una competición publicada, se omite
  sin error y podrá capturarse en una ejecución posterior.
- Todas las fuentes/parsers se validan ANTES de abrir el lote de escritura.
- PROGRAMADO pasa a FINALIZADO en reruns, sin duplicados.
- No se crean rivales externos en nucleo_equipos.
- IDs de partido estables e independientes de la fuente concreta.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta


BASE = "https://www.soccerdonna.de"
FUENTE_NORMALIZADA = "fem-complement"
FUENTE_RAW = "soccerdonna.de"

TEMPORADAS = ("2022-23", "2023-24", "2024-25", "2025-26", "2026-27")
TEMPORADA_ACTUAL = "2026-27"

COMPETICIONES = {
    "copa": "Copa de la Reina",
    "supercopa": "Supercopa Femenina",
    "uwcl": "UEFA Women's Champions League",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Quiniela1X2-Femenino/1.0; "
        "+https://1x2.juancarlosmallo.com)"
    ),
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8,de;q=0.5",
}

DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*:\s*(\d{1,2})(?!\d)")
TEAM_ID_RE = re.compile(r"verein_(\d+)(?:_|\.html|$)", re.I)
MATCH_ID_RE = re.compile(r"spielbericht_(\d+)\.html", re.I)

# Alias de nombres observados en SoccerDonna hacia los canónicos de la BD.
# El ID SoccerDonna es prioritario; esto es fallback.
ALIASES = {
    "fc barcelona": "FC Barcelona Femení",
    "f c barcelona": "FC Barcelona Femení",
    "real madrid": "Real Madrid CF Femenino",
    "real madrid cf": "Real Madrid CF Femenino",
    "atletico madrid": "Atlético de Madrid Femenino",
    "atletico de madrid": "Atlético de Madrid Femenino",
    "club atletico de madrid": "Atlético de Madrid Femenino",
    "athletic club": "Athletic Club Femenino",
    "athletic bilbao": "Athletic Club Femenino",
    "real sociedad": "Real Sociedad Femenino",
    "real socieadad": "Real Sociedad Femenino",
    "madrid cff": "Madrid CFF",
    "ud tenerife": "Costa Adeje Tenerife",
    "udg tenerife sur": "Costa Adeje Tenerife",
    "cd tenerife femenino": "Costa Adeje Tenerife",
    "tenerife fem": "Costa Adeje Tenerife",
    "costa adeje tenerife": "Costa Adeje Tenerife",
    "sevilla fc": "Sevilla FC Femenino",
    "f c sevilla": "Sevilla FC Femenino",
    "valencia feminas cf": "Valencia CF Femenino",
    "valencia feminas club de futbol": "Valencia CF Femenino",
    "levante ud": "Levante UD Femenino",
    "sdf real betis balompie": "Real Betis Féminas",
    "real betis balompie": "Real Betis Féminas",
    "real betis feminas": "Real Betis Féminas",
    "cd sporting club de huelva": "Sporting Club Huelva",
    "sporting de huelva": "Sporting Club Huelva",
    "sport huelva": "Sporting Club Huelva",
    "villarreal cf": "Villarreal CF Femenino",
    "alhama cf": "Alhama CF ElPozo",
    "deportivo alaves": "Deportivo Alavés Femenino",
    "deportivo alaves gloriosas": "Deportivo Alavés Femenino",
    "sd eibar": "SD Eibar Femenino",
    "granada cf": "Granada CF Femenino",
    "fc domont": "Granada CF Femenino",
    "rcd espanyol": "RCD Espanyol Femenino",
    "espanyol": "RCD Espanyol Femenino",
    "deportivo la coruna": "Deportivo Abanca",
    "rc deportivo a coruna": "Deportivo Abanca",
    "deportivo abanca": "Deportivo Abanca",
    "fc levante las planas": "FC Badalona Women",
    "fc levante badalona": "FC Badalona Women",
    "fc badalona women": "FC Badalona Women",
    "badalona women": "FC Badalona Women",
    "dux logrono": "Logroño United",
    "logrono united": "Logroño United",
    "edf logrono": "Logroño United",
}


@dataclass
class Equipo:
    equipo_id: int
    nombre: str
    soccerdonna_id: str | None = None


@dataclass
class Partido:
    competicion: str
    ronda: str | None
    fecha_sql: str
    hora_confirmada: bool
    estado: str
    local: str
    visitante: str
    local_sd_id: str | None
    visitante_sd_id: str | None
    equipo_local_id: int | None
    equipo_visitante_id: int | None
    goles_local: int | None
    goles_visitante: int | None
    hubo_prorroga: bool
    hubo_penaltis: bool
    resultado_raw: str | None
    url: str
    es_clasificatoria: bool = False
    fuente_raw: str = FUENTE_RAW


def ahora_sql() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def temporada_rango(etiqueta: str) -> tuple[datetime, datetime]:
    m = re.fullmatch(r"(20\d{2})-(\d{2})", etiqueta)
    if not m:
        raise RuntimeError(f"Temporada inválida: {etiqueta}")
    inicio = int(m.group(1))
    fin = inicio + 1
    if fin % 100 != int(m.group(2)):
        raise RuntimeError(f"Temporada no consecutiva: {etiqueta}")
    return datetime(inicio, 7, 1), datetime(fin, 6, 30, 23, 59, 59)


def session_sd() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_retry(
    session: requests.Session,
    url: str,
    *,
    permitir_404: bool = False,
) -> requests.Response | None:
    ultimo: Exception | None = None

    for intento in range(1, 4):
        try:
            r = session.get(url, timeout=(10, 45), allow_redirects=True)

            if r.status_code == 404 and permitir_404:
                return None

            if r.status_code in {429, 500, 502, 503, 504}:
                ultimo = RuntimeError(f"HTTP {r.status_code}")
                if intento < 3:
                    time.sleep(2 ** (intento - 1))
                    continue

            r.raise_for_status()

            if len(r.text) < 500:
                raise RuntimeError(
                    f"HTML demasiado corto ({len(r.text)} bytes)"
                )
            return r

        except (requests.Timeout, requests.ConnectionError, RuntimeError) as exc:
            ultimo = exc
            if intento < 3:
                time.sleep(2 ** (intento - 1))
                continue

    if permitir_404:
        return None
    raise RuntimeError(f"No pude descargar {url}: {ultimo}")


def indice_equipos(
    equipos: list[Equipo],
) -> tuple[dict[str, Equipo], dict[str, Equipo]]:
    por_id: dict[str, Equipo] = {}
    por_nombre: dict[str, Equipo] = {}

    for e in equipos:
        por_nombre[norm(e.nombre)] = e
        if e.soccerdonna_id:
            por_id[str(e.soccerdonna_id)] = e

    # Alias únicamente si el canónico realmente existe en nuestro universo.
    por_canon = {e.nombre: e for e in equipos}
    for alias, canonico in ALIASES.items():
        e = por_canon.get(canonico)
        if e is not None:
            por_nombre[norm(alias)] = e

    return por_id, por_nombre


def resolver_equipo(
    nombre: str,
    sd_id: str | None,
    por_id: dict[str, Equipo],
    por_nombre: dict[str, Equipo],
) -> Equipo | None:
    if sd_id and sd_id in por_id:
        return por_id[sd_id]

    n = norm(nombre)
    if n in por_nombre:
        return por_nombre[n]

    # Fallback prudente: inclusión solo para nombres razonablemente largos.
    if len(n) >= 7:
        candidatos = []
        for clave, e in por_nombre.items():
            if len(clave) >= 7 and (n in clave or clave in n):
                candidatos.append(e)
        unicos = {e.equipo_id: e for e in candidatos}
        if len(unicos) == 1:
            return next(iter(unicos.values()))

    return None


def nombre_anchor(a) -> str:
    txt = " ".join(a.stripped_strings).strip()
    if txt:
        return txt
    img = a.find("img")
    if img is not None:
        alt = (img.get("alt") or "").strip()
        if alt:
            return alt
        title = (img.get("title") or "").strip()
        if title:
            return title
    return (a.get("title") or "").strip()


def equipos_de_fila(row) -> list[tuple[str, str | None]]:
    """
    Devuelve los dos clubes de una fila SoccerDonna.

    Prioridad: enlaces con verein_ID, porque son muchísimo más estables que
    los nombres ('Real Socieadad', 'FC Domont', etc.).
    """
    por_id: dict[str, str] = {}

    for a in row.find_all("a", href=True):
        href = str(a.get("href", ""))
        m = TEAM_ID_RE.search(href)
        if not m:
            continue
        ident = m.group(1)
        nombre = nombre_anchor(a)
        if ident not in por_id or (nombre and not por_id[ident]):
            por_id[ident] = nombre

    if len(por_id) >= 2:
        pares = list(por_id.items())[:2]
        return [(nombre or f"SoccerDonna {ident}", ident) for ident, nombre in pares]

    # Fallback de estructura de tabla.
    celdas = [td.get_text(" ", strip=True) for td in row.find_all("td")]
    limpios = []
    for txt in celdas:
        if not txt:
            continue
        if DATE_RE.search(txt):
            continue
        if SCORE_RE.search(txt) or "-:-" in txt:
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}(?:\s*Uhr)?", txt):
            continue
        if len(txt) <= 2:
            continue
        limpios.append(txt)

    if len(limpios) >= 2:
        return [(limpios[0], None), (limpios[-1], None)]

    return []


def fecha_hora_fila(row) -> tuple[str, bool] | None:
    celdas = [td.get_text(" ", strip=True) for td in row.find_all("td")]
    if not celdas:
        return None

    fecha_match = None
    fecha_idx = None

    for i, txt in enumerate(celdas[:3]):
        m = DATE_RE.search(txt)
        if m:
            fecha_match = m
            fecha_idx = i
            break

    if fecha_match is None or fecha_idx is None:
        return None

    dia, mes, anyo = map(int, fecha_match.groups())

    # Buscar hora solo en la celda de fecha o inmediatamente siguiente.
    hora = None
    for txt in celdas[fecha_idx : min(len(celdas), fecha_idx + 2)]:
        # Eliminar la fecha para no confundir nada posterior.
        sin_fecha = DATE_RE.sub(" ", txt)
        tm = TIME_RE.search(sin_fecha)
        if tm:
            hora = (int(tm.group(1)), int(tm.group(2)))
            break

    if hora is None:
        # Nunca fingimos una hora exacta.
        hh, mm = 12, 0
        confirmada = False
    else:
        hh, mm = hora
        confirmada = True

    dt = datetime(anyo, mes, dia, hh, mm)
    return dt.strftime("%Y-%m-%d %H:%M:%S"), confirmada


def resultado_fila(row) -> tuple[str, int, int, bool, bool] | None:
    celdas = [td.get_text(" ", strip=True) for td in row.find_all("td")]

    # De derecha a izquierda para evitar que una hora tipo 19:00 se confunda
    # con un marcador.
    for txt in reversed(celdas):
        if DATE_RE.search(txt):
            continue
        m = SCORE_RE.search(txt)
        if not m:
            continue

        gl, gv = int(m.group(1)), int(m.group(2))
        n = norm(txt)
        prorroga = (
            "a e t" in n
            or "n v" in n
            or "extra time" in n
        )
        penaltis = (
            "pen" in n
            or "n e" in n
            or "elfmeter" in n
        )
        return txt[:100], gl, gv, prorroga, penaltis

    return None


def ronda_fila(row) -> str | None:
    h = row.find_previous(["h2", "h3", "h4"])
    if h is None:
        return None
    txt = h.get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:150] or None


def url_partido_fila(row, fallback_url: str) -> str:
    for a in row.find_all("a", href=True):
        href = str(a["href"])
        if MATCH_ID_RE.search(href):
            return urljoin(BASE, href)
    return fallback_url


def parse_competicion_html(
    html: str,
    *,
    temporada: str,
    competicion: str,
    url: str,
    por_id: dict[str, Equipo],
    por_nombre: dict[str, Equipo],
    es_clasificatoria: bool = False,
    actual: bool = False,
) -> tuple[list[Partido], int]:
    """
    Devuelve (partidos_relevantes, filas_de_partido_parseadas).

    filas_de_partido_parseadas se usa como guard de estructura: permite
    detectar que SoccerDonna ha cambiado HTML antes de escribir.
    """
    soup = BeautifulSoup(html, "html.parser")
    ini, fin = temporada_rango(temporada)

    relevantes: list[Partido] = []
    filas_parseadas = 0
    ahora = datetime.now()

    for row in soup.find_all("tr"):
        fh = fecha_hora_fila(row)
        if fh is None:
            continue

        fecha_sql, hora_confirmada = fh
        dt = datetime.strptime(fecha_sql, "%Y-%m-%d %H:%M:%S")

        # Rechazar páginas de otra temporada (por ejemplo, redirect al current).
        if not (ini <= dt <= fin):
            continue

        eqs = equipos_de_fila(row)
        if len(eqs) < 2:
            continue

        local, local_sd = eqs[0]
        visitante, visitante_sd = eqs[1]

        filas_parseadas += 1

        res = resultado_fila(row)
        if res is None:
            # En pasado, una fila sin marcador es una fuente incompleta.
            # No inventamos un PROGRAMADO atrasado.
            if actual and dt < ahora:
                continue
            estado = "PROGRAMADO"
            resultado_raw = None
            gl = gv = None
            pro = pen = False
        else:
            resultado_raw, gl, gv, pro, pen = res
            estado = "FINALIZADO"

        ldb = resolver_equipo(local, local_sd, por_id, por_nombre)
        vdb = resolver_equipo(visitante, visitante_sd, por_id, por_nombre)

        if ldb is None and vdb is None:
            continue

        relevantes.append(
            Partido(
                competicion=competicion,
                ronda=ronda_fila(row),
                fecha_sql=fecha_sql,
                hora_confirmada=hora_confirmada,
                estado=estado,
                local=local,
                visitante=visitante,
                local_sd_id=local_sd,
                visitante_sd_id=visitante_sd,
                equipo_local_id=ldb.equipo_id if ldb else None,
                equipo_visitante_id=vdb.equipo_id if vdb else None,
                goles_local=gl,
                goles_visitante=gv,
                hubo_prorroga=pro,
                hubo_penaltis=pen,
                resultado_raw=resultado_raw,
                url=url_partido_fila(row, url),
                es_clasificatoria=es_clasificatoria,
            )
        )

    return relevantes, filas_parseadas


def urls_candidatas(clave: str, anio: int, *, qualifying: bool = False) -> list[str]:
    if clave == "copa":
        return [
            f"{BASE}/en/copa-de-la-reina/gruppenspieltage/pokalwettbewerb_ESPP_{anio}.html",
            f"{BASE}/de/copa-de-la-reina/gruppenspieltage/pokalwettbewerb_ESPP_{anio}.html",
            f"{BASE}/en/copa-de-la-reina/startseite/pokalwettbewerb_ESPP_{anio}_4.html",
        ]

    if clave == "supercopa":
        return [
            f"{BASE}/en/supercopa-femenina/gruppenspieltage/pokalwettbewerb_ESPS_{anio}.html",
            f"{BASE}/de/supercopa-femenina/gruppenspieltage/pokalwettbewerb_ESPS_{anio}.html",
            f"{BASE}/en/supercopa-femenina/startseite/pokalwettbewerb_ESPS_{anio}_4.html",
            f"{BASE}/en/supercopa-femenina/startseite/wettbewerb_ESPS_{anio}.html",
        ]

    if clave == "uwcl" and not qualifying:
        return [
            f"{BASE}/en/uefa-womens-champions-league/gruppenspieltage/pokalwettbewerb_CL_{anio}.html",
            f"{BASE}/de/uefa-womens-champions-league/gruppenspieltage/pokalwettbewerb_CL_{anio}.html",
            f"{BASE}/en/champoins-league/gruppenspieltage/pokalwettbewerb_CL_{anio}.html",
            f"{BASE}/en/uefa-womens-champions-league/startseite/pokalwettbewerb_CL_{anio}_4.html",
        ]

    if clave == "uwcl" and qualifying:
        return [
            f"{BASE}/en/uefa-womens-champions-league-qualifying/gruppenspieltage/pokalwettbewerb_CLQ_{anio}.html",
            f"{BASE}/de/uefa-womens-champions-league-qualifying/gruppenspieltage/pokalwettbewerb_CLQ_{anio}.html",
            f"{BASE}/en/uefa-womens-champions-league-qualifying/startseite/pokalwettbewerb_CLQ_{anio}_4.html",
            f"{BASE}/en/uefa-womens-champions-league-quali/startseite/wettbewerb_CLQ_{anio}.html",
        ]

    raise ValueError((clave, qualifying))


def minimo_historico(clave: str, *, qualifying: bool = False) -> int:
    if clave == "copa":
        return 20
    if clave == "supercopa":
        return 3
    if clave == "uwcl" and qualifying:
        return 15
    if clave == "uwcl":
        return 50
    return 1


def cargar_fuente_sd(
    session: requests.Session,
    *,
    clave: str,
    temporada: str,
    por_id: dict[str, Equipo],
    por_nombre: dict[str, Equipo],
    qualifying: bool = False,
) -> tuple[list[Partido], tuple[str, str] | None]:
    anio = int(temporada[:4])
    actual = temporada == TEMPORADA_ACTUAL
    comp = COMPETICIONES[clave]

    errores = []
    mejor: tuple[list[Partido], int, str, str] | None = None

    for url in urls_candidatas(clave, anio, qualifying=qualifying):
        print(f"  probando fuente: {url}")
        try:
            r = get_retry(session, url, permitir_404=True)
            if r is None:
                errores.append(f"{url}: 404/no disponible")
                continue

            ps, filas = parse_competicion_html(
                r.text,
                temporada=temporada,
                competicion=comp,
                url=r.url,
                por_id=por_id,
                por_nombre=por_nombre,
                es_clasificatoria=qualifying,
                actual=actual,
            )

            print(
                f"    filas de partido reconocidas={filas}; "
                f"relevantes={len(ps)}"
            )

            if mejor is None or filas > mejor[1]:
                mejor = (ps, filas, r.url, r.text)

            if actual:
                # En la temporada actual aceptamos una página parcial.
                if filas > 0:
                    return ps, (r.url, r.text)
                continue

            if filas >= minimo_historico(clave, qualifying=qualifying):
                return ps, (r.url, r.text)

            errores.append(
                f"{r.url}: solo {filas} filas; "
                f"mínimo={minimo_historico(clave, qualifying=qualifying)}"
            )

        except Exception as exc:
            errores.append(f"{url}: {exc}")

    if actual:
        print(
            f"  {comp}{' qualifying' if qualifying else ''}: "
            "todavía sin una página utilizable; se omite sin error."
        )
        if mejor is not None and mejor[1] > 0:
            return mejor[0], (mejor[2], mejor[3])
        return [], None

    raise RuntimeError(
        f"No encontré una fuente SoccerDonna válida para {comp} "
        f"{temporada}{' qualifying' if qualifying else ''}. "
        + " | ".join(errores[:8])
    )


def id_estable(p: Partido, temporada: str) -> str:
    """
    ID independiente de SoccerDonna/UEFA.

    No incluye la hora, para que un cambio posterior de 18:45 a 21:00
    actualice el mismo partido en lugar de duplicarlo.
    """
    dia = p.fecha_sql[:10]
    semilla = "|".join(
        [
            temporada,
            p.competicion,
            "Q" if p.es_clasificatoria else "M",
            dia,
            norm(p.local),
            norm(p.visitante),
        ]
    )
    return hashlib.sha256(semilla.encode("utf-8")).hexdigest()[:48]


def clave_semantica(p: Partido) -> tuple:
    """
    Identidad lógica para deduplicar ANTES de escribir.

    Problema que corrige:
    SoccerDonna y el fallback UEFA pueden llamar al mismo rival de forma
    distinta ("Ajax" / "Ajax Amsterdam", "Chelsea" / "Chelsea FC").
    Si usamos los nombres como clave, el mismo encuentro pasa dos veces.

    Para partidos con un solo equipo de nuestro universo, competición +
    fecha + lado + equipo_id identifican inequívocamente el encuentro:
    un club no juega dos partidos de la misma competición el mismo día.

    Si ambos equipos son seguidos, usamos ambos IDs y sus lados.

    IMPORTANTE: no cambiamos id_estable() de los registros persistidos.
    Así un rerun actualiza una de las filas ya existentes y no crea un
    tercer ID distinto mientras limpiamos los duplicados antiguos.
    """
    base = (
        p.competicion,
        p.fecha_sql[:10],
        p.es_clasificatoria,
    )

    if p.equipo_local_id is not None and p.equipo_visitante_id is not None:
        return base + (
            "AMBOS",
            int(p.equipo_local_id),
            int(p.equipo_visitante_id),
        )

    if p.equipo_local_id is not None:
        return base + ("LOCAL_SEGUIDO", int(p.equipo_local_id))

    if p.equipo_visitante_id is not None:
        return base + ("VISITANTE_SEGUIDO", int(p.equipo_visitante_id))

    # No debería llegar aquí: el preflight prohíbe partidos sin equipo seguido.
    return base + ("SIN_EQUIPO", norm(p.local), norm(p.visitante))


def calidad(p: Partido) -> tuple[int, int]:
    return (
        1 if p.estado == "FINALIZADO" else 0,
        1 if p.hora_confirmada else 0,
    )


def deduplicar(partidos: list[Partido]) -> list[Partido]:
    out: dict[tuple[str, str, str, str, bool], Partido] = {}

    for p in partidos:
        k = clave_semantica(p)
        previo = out.get(k)
        if previo is None or calidad(p) > calidad(previo):
            out[k] = p

    return list(out.values())


def fallbacks_uwcl_2026_27(
    por_id: dict[str, Equipo],
    por_nombre: dict[str, Equipo],
) -> list[Partido]:
    """
    Resultados ya disputados por los equipos españoles en la 3ª ronda
    de clasificación 2026-27, verificados antes de publicar este paquete.

    Actúan como respaldo si SoccerDonna tarda en actualizar un marcador.
    Cuando SoccerDonna esté actualizado, deduplicar() conserva una única fila.
    """
    filas = [
        (
            "2026-08-26 19:30:00",
            "Ajax Amsterdam",
            "Real Madrid CF",
            0, 2,
            "https://www.uefa.com/womenschampionsleague/",
        ),
        (
            "2026-08-26 19:45:00",
            "Chelsea FC",
            "Real Sociedad",
            5, 2,
            "https://www.uefa.com/womenschampionsleague/",
        ),
        (
            "2026-09-02 19:00:00",
            "Real Sociedad",
            "Chelsea FC",
            0, 1,
            "https://www.uefa.com/womenschampionsleague/",
        ),
        (
            "2026-09-02 20:00:00",
            "Real Madrid CF",
            "Ajax Amsterdam",
            2, 1,
            "https://www.uefa.com/womenschampionsleague/",
        ),
    ]

    out = []
    for fecha, local, visitante, gl, gv, url in filas:
        ldb = resolver_equipo(local, None, por_id, por_nombre)
        vdb = resolver_equipo(visitante, None, por_id, por_nombre)

        if ldb is None and vdb is None:
            continue

        out.append(
            Partido(
                competicion=COMPETICIONES["uwcl"],
                ronda="third qualifying round",
                fecha_sql=fecha,
                hora_confirmada=True,
                estado="FINALIZADO",
                local=local,
                visitante=visitante,
                local_sd_id=None,
                visitante_sd_id=None,
                equipo_local_id=ldb.equipo_id if ldb else None,
                equipo_visitante_id=vdb.equipo_id if vdb else None,
                goles_local=gl,
                goles_visitante=gv,
                hubo_prorroga=False,
                hubo_penaltis=False,
                resultado_raw=f"{gl}:{gv}",
                url=url,
                es_clasificatoria=True,
                fuente_raw="uefa.com",
            )
        )

    return out


def cargar_contexto(api: ApiIngesta) -> list[Equipo]:
    data = api._request_json(
        "GET",
        "contexto_equipos_complementarios_femeninos.php",
        timeout=45,
    )
    items = data.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError("Contexto femenino con formato inválido.")

    equipos = []
    for x in items:
        equipos.append(
            Equipo(
                equipo_id=int(x["equipo_id"]),
                nombre=str(x["nombre_canonico"]),
                soccerdonna_id=(
                    str(x["soccerdonna_id"])
                    if x.get("soccerdonna_id")
                    else None
                ),
            )
        )

    if len(equipos) < 16:
        raise RuntimeError(
            f"El universo femenino solo contiene {len(equipos)} equipos; "
            "esperaba al menos los 16 de una temporada Liga F."
        )

    return equipos


def claves_competicion(arg: str) -> list[str]:
    if arg == "todas":
        return ["copa", "supercopa", "uwcl"]
    return [arg]


def preflight_temporada(
    session: requests.Session,
    *,
    temporada: str,
    claves: list[str],
    por_id: dict[str, Equipo],
    por_nombre: dict[str, Equipo],
) -> tuple[list[Partido], list[tuple[str, str, str]]]:
    """
    Descarga y valida TODO antes de abrir lote.

    raws: (fuente, url, contenido)
    """
    partidos: list[Partido] = []
    raws: list[tuple[str, str, str]] = []

    for clave in claves:
        print("=" * 68)
        print(f"{temporada} - {COMPETICIONES[clave]}")
        print("=" * 68)

        ps, raw = cargar_fuente_sd(
            session,
            clave=clave,
            temporada=temporada,
            por_id=por_id,
            por_nombre=por_nombre,
            qualifying=False,
        )
        partidos.extend(ps)

        if raw is not None:
            raws.append((FUENTE_RAW, raw[0], raw[1]))

        if clave == "uwcl":
            qps, qraw = cargar_fuente_sd(
                session,
                clave=clave,
                temporada=temporada,
                por_id=por_id,
                por_nombre=por_nombre,
                qualifying=True,
            )
            partidos.extend(qps)

            if qraw is not None:
                raws.append((FUENTE_RAW, qraw[0], qraw[1]))

            if temporada == TEMPORADA_ACTUAL:
                fb = fallbacks_uwcl_2026_27(por_id, por_nombre)
                print(
                    f"  respaldo UEFA 2026-27: {len(fb)} partidos "
                    "españoles de clasificación ya jugados"
                )
                partidos.extend(fb)

    antes_dedupe = len(partidos)
    partidos = deduplicar(partidos)
    eliminados_dedupe = antes_dedupe - len(partidos)
    if eliminados_dedupe:
        print(
            f"  deduplicación semántica: {eliminados_dedupe} fila(s) "
            "equivalentes colapsadas antes de escribir"
        )

    # Protección definitiva: nunca mandar a la API un partido no relacionado.
    for p in partidos:
        if p.equipo_local_id is None and p.equipo_visitante_id is None:
            raise RuntimeError(
                f"BUG filtro: {p.local}-{p.visitante} no tiene equipo seguido."
            )

    return partidos, raws


def payload_partido(
    p: Partido,
    *,
    temporada: str,
    lote: int,
) -> dict:
    return {
        "lote_id": lote,
        "fuente": FUENTE_NORMALIZADA,
        "id_partido_fuente": id_estable(p, temporada),
        "temporada": temporada,
        "competicion": p.competicion,
        "ronda": p.ronda,
        "es_clasificatoria": p.es_clasificatoria,
        "fecha_hora_inicio": p.fecha_sql,
        "hora_confirmada": p.hora_confirmada,
        "estado": p.estado,
        "equipo_local_id": p.equipo_local_id,
        "equipo_visitante_id": p.equipo_visitante_id,
        "local_nombre": p.local,
        "visitante_nombre": p.visitante,
        "local_id_fuente": p.local_sd_id,
        "visitante_id_fuente": p.visitante_sd_id,
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
        choices=("todas", "copa", "supercopa", "uwcl"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api = ApiIngesta()
    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    equipos = cargar_contexto(api)
    con_sd = sum(bool(e.soccerdonna_id) for e in equipos)

    print(
        f"Universo femenino: {len(equipos)} equipos históricos Liga F; "
        f"{con_sd} con ID SoccerDonna guardado."
    )

    por_id, por_nombre = indice_equipos(equipos)
    session = session_sd()

    # PRE-FLIGHT COMPLETO. Si falla, no existe lote y no se escribe nada.
    partidos, raws = preflight_temporada(
        session,
        temporada=args.temporada,
        claves=claves_competicion(args.competicion),
        por_id=por_id,
        por_nombre=por_nombre,
    )

    finalizados = sum(p.estado == "FINALIZADO" for p in partidos)
    programados = sum(p.estado == "PROGRAMADO" for p in partidos)
    estimados = sum(not p.hora_confirmada for p in partidos)

    print("\nPRE-FLIGHT OK")
    print(
        f"Relevantes={len(partidos)} | finalizados={finalizados} | "
        f"programados={programados} | hora_no_confirmada={estimados}"
    )

    por_comp: dict[str, int] = {}
    for p in partidos:
        por_comp[p.competicion] = por_comp.get(p.competicion, 0) + 1
    for comp, n in sorted(por_comp.items()):
        print(f"  {comp}: {n}")

    if args.dry_run:
        print("DRY-RUN OK. No se escribió nada en IONOS.")
        return

    if not partidos:
        print(
            "OK: todavía no hay partidos relevantes cargables para la "
            "selección. No se abre lote vacío."
        )
        return

    # tipo_fuente <= 20: aprendido del error masculino.
    lote = api.iniciar_lote(
        fuente="fem-complement",
        tipo_fuente="fem_comp",
        notas=(
            f"Competiciones femeninas {args.temporada}; "
            f"seleccion={args.competicion}; "
            "Copa+Supercopa+UWCL; no crear rivales externos"
        ),
    )
    print("Lote abierto:", lote)

    creados = actualizados = errores = 0
    mensajes: list[str] = []

    try:
        # RAW solo después de haber pasado todo el preflight.
        for fuente, url, contenido in raws:
            api.guardar_documento(
                {
                    "lote_id": lote,
                    "fuente": fuente,
                    "url": url,
                    "tipo_contenido": "sd_comp_fem",
                    "obtenido_en": ahora_sql(),
                    "contenido": contenido,
                }
            )

        for p in sorted(
            partidos,
            key=lambda x: (
                x.fecha_sql,
                x.competicion,
                x.local,
                x.visitante,
            ),
        ):
            try:
                res = api._request_json(
                    "POST",
                    "guardar_partido_complementario_femenino.php",
                    json=payload_partido(
                        p,
                        temporada=args.temporada,
                        lote=lote,
                    ),
                    timeout=60,
                )

                if res.get("accion") == "creado":
                    creados += 1
                else:
                    actualizados += 1

                marcador = (
                    f"{p.goles_local}-{p.goles_visitante}"
                    if p.goles_local is not None
                    and p.goles_visitante is not None
                    else "?-?"
                )

                print(
                    f"{p.fecha_sql[:16]} [{p.competicion}] "
                    f"{p.local} {marcador} {p.visitante} "
                    f"[{p.estado}] -> {res.get('accion')}"
                )

            except Exception as exc:
                errores += 1
                msg = (
                    f"{p.competicion} {p.fecha_sql[:10]} "
                    f"{p.local}-{p.visitante}: {exc}"
                )
                mensajes.append(msg)
                print("ERROR guardando:", msg)

        estado = "completado" if errores == 0 else "error"

        api.finalizar_lote(
            lote,
            estado=estado,
            notas=(
                f"partidos={len(partidos)}; creados={creados}; "
                f"actualizados={actualizados}; errores={errores}"
            ),
        )

    except Exception:
        # Intentar cerrar el lote como error, pero conservar la excepción real.
        try:
            api.finalizar_lote(
                lote,
                estado="error",
                notas=(
                    f"Fallo durante escritura; creados={creados}; "
                    f"actualizados={actualizados}; errores={errores}"
                ),
            )
        except Exception:
            pass
        raise

    print(
        f"\nRESUMEN {args.temporada}: "
        f"creados={creados}, actualizados={actualizados}, errores={errores}"
    )

    if mensajes:
        print("\nErrores:")
        for msg in mensajes:
            print(" -", msg)

    if errores:
        raise RuntimeError(
            f"La carga terminó con {errores} errores de guardado."
        )


if __name__ == "__main__":
    main()
