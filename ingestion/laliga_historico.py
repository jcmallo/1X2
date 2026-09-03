"""
Backfill histórico de Primera y Segunda desde las páginas oficiales de resultados
por jornada de laliga.com.

Diseñado para 2022-23 .. 2025-26. No toca el ingestor incremental actual de
2026-27.

La página de jornada histórica es la autoridad para:
- jornada
- fecha/hora
- local/visitante
- resultado final
- IDs/slugs de club de laliga.com

No depende de la ficha individual del partido: algunos enlaces históricos de
2022-23 ya devuelven 404 aunque la página de jornada siga disponible.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from api_client import ApiIngesta

BASE = "https://www.laliga.com"
FUENTE = "laliga.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Quiniela1X2-Historico/1.0; "
        "+https://1x2.juancarlosmallo.com)"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

DIVISIONES = {
    "primera": {
        "slug": "laliga-easports",
        "nombre": "LaLiga",
        "nivel": "PRIMERA_CATEGORIA",
        "numero_equipos": 20,
        "numero_jornadas": 38,
        "partidos_jornada": 10,
    },
    "segunda": {
        "slug": "laliga-hypermotion",
        "nombre": "Segunda División",
        "nivel": "SEGUNDA_CATEGORIA",
        "numero_equipos": 22,
        "numero_jornadas": 42,
        "partidos_jornada": 11,
    },
}

PATRON_FECHA = re.compile(
    r"(?:LUN|MAR|MI[ÉE]|JUE|VIE|S[ÁA]B|DOM)\s+"
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})",
    re.I,
)
PATRON_HORA = re.compile(r"\b(\d{1,2}):(\d{2})\b")
PATRON_MARCADOR = re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\b")
PATRON_CLUB = re.compile(r"/clubes/([^/?#]+)", re.I)


@dataclass(frozen=True)
class PartidoJornada:
    jornada: int
    fecha_hora: str
    local: str
    visitante: str
    local_slug: str
    visitante_slug: str
    goles_local: int
    goles_visitante: int
    fragmento: str


def ahora_sql() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def simplificar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.casefold()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def validar_temporada(etiqueta: str) -> tuple[int, int]:
    m = re.fullmatch(r"(20\d{2})-(\d{2})", etiqueta.strip())
    if not m:
        raise RuntimeError("Temporada inválida. Usa por ejemplo 2022-23.")
    inicio = int(m.group(1))
    fin = inicio + 1
    if fin % 100 != int(m.group(2)):
        raise RuntimeError("La temporada no es consecutiva: " + etiqueta)
    return inicio, fin


def jornadas_desde_texto(raw: str | None, max_jornada: int) -> list[int]:
    if not raw:
        return list(range(1, max_jornada + 1))

    resultado: set[int] = set()
    for trozo in raw.split(","):
        trozo = trozo.strip()
        if not trozo:
            continue
        if "-" in trozo:
            a, b = trozo.split("-", 1)
            desde, hasta = int(a), int(b)
            if desde > hasta:
                desde, hasta = hasta, desde
            resultado.update(range(desde, hasta + 1))
        else:
            resultado.add(int(trozo))

    if not resultado or min(resultado) < 1 or max(resultado) > max_jornada:
        raise RuntimeError(f"Jornadas válidas: 1..{max_jornada}.")
    return sorted(resultado)


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


def club_info(a: Tag) -> tuple[str, str] | None:
    href = str(a.get("href") or "").strip()
    m = PATRON_CLUB.search(urlparse(urljoin(BASE, href)).path)
    if not m:
        return None
    nombre = a.get_text(" ", strip=True)
    if not nombre:
        return None
    return nombre, m.group(1).strip().lower()


def clubes_unicos(contenedor: Tag) -> list[tuple[str, str]]:
    salida: list[tuple[str, str]] = []
    vistos: set[str] = set()
    for a in contenedor.find_all("a", href=True):
        info = club_info(a)
        if not info:
            continue
        nombre, slug = info
        if slug in vistos:
            continue
        vistos.add(slug)
        salida.append((nombre, slug))
    return salida


def contenedor_partido(a: Tag) -> Tag | None:
    """Devuelve el ancestro más pequeño que contiene una fila de partido."""
    mejor: Tag | None = None
    for ancestro in a.parents:
        if not isinstance(ancestro, Tag) or ancestro.name in {"html", "body"}:
            break
        texto = " ".join(ancestro.stripped_strings)
        if not PATRON_FECHA.search(texto):
            continue
        if not PATRON_HORA.search(texto):
            continue
        if not PATRON_MARCADOR.search(texto):
            continue
        clubes = clubes_unicos(ancestro)
        if len(clubes) == 2:
            return ancestro
        if 2 < len(clubes) <= 4 and mejor is None:
            mejor = ancestro
    return mejor


def parsear_contenedor(contenedor: Tag, jornada: int) -> PartidoJornada:
    texto = " ".join(contenedor.stripped_strings)
    mf = PATRON_FECHA.search(texto)
    mh = PATRON_HORA.search(texto)
    mm = PATRON_MARCADOR.search(texto)
    clubes = clubes_unicos(contenedor)

    if not mf or not mh or not mm or len(clubes) < 2:
        raise RuntimeError("Fila histórica incompleta.")

    # En el ancestro mínimo, los dos primeros clubes únicos son local/visitante.
    (local, local_slug), (visitante, visitante_slug) = clubes[:2]
    dia, mes, anio = int(mf.group(1)), int(mf.group(2)), int(mf.group(3))
    hora, minuto = int(mh.group(1)), int(mh.group(2))

    return PartidoJornada(
        jornada=jornada,
        fecha_hora=f"{anio:04d}-{mes:02d}-{dia:02d} {hora:02d}:{minuto:02d}:00",
        local=local,
        visitante=visitante,
        local_slug=local_slug,
        visitante_slug=visitante_slug,
        goles_local=int(mm.group(1)),
        goles_visitante=int(mm.group(2)),
        fragmento=texto[:5000],
    )


def extraer_partidos(html: str, jornada: int) -> list[PartidoJornada]:
    soup = BeautifulSoup(html, "html.parser")
    encontrados: dict[tuple[str, str], PartidoJornada] = {}

    for a in soup.find_all("a", href=True):
        info = club_info(a)
        if not info:
            continue
        cont = contenedor_partido(a)
        if cont is None:
            continue
        try:
            p = parsear_contenedor(cont, jornada)
        except RuntimeError:
            continue
        encontrados[(p.local_slug, p.visitante_slug)] = p

    return sorted(
        encontrados.values(),
        key=lambda p: (p.fecha_hora, p.local_slug, p.visitante_slug),
    )


def id_partido_fuente(temporada: str, division: str, p: PartidoJornada) -> str:
    return (
        f"hist-{temporada}-{division}-j{p.jornada:02d}-"
        f"{p.local_slug}-{p.visitante_slug}"
    )[:180]


def payload_partido(
    *,
    lote: int,
    temporada: str,
    division: str,
    cfg: dict,
    p: PartidoJornada,
    url_jornada: str,
    temporada_inicio: str,
) -> dict:
    raw = json.dumps(
        {
            "fuente": FUENTE,
            "url_jornada": url_jornada,
            "temporada": temporada,
            "division": division,
            "jornada": p.jornada,
            "fecha_hora": p.fecha_hora,
            "local": p.local,
            "visitante": p.visitante,
            "goles_local": p.goles_local,
            "goles_visitante": p.goles_visitante,
            "fragmento_texto": p.fragmento,
        },
        ensure_ascii=False,
    )

    return {
        "lote_id": lote,
        "fuente": FUENTE,
        "id_partido_fuente": id_partido_fuente(temporada, division, p),
        "competicion_nombre": cfg["nombre"],
        "competicion_pais": "España",
        "competicion_genero": "MASCULINO",
        "competicion_nivel": cfg["nivel"],
        "competicion_organizador": "LaLiga",
        "competicion_apta_quiniela": 1,
        "competicion_numero_equipos": cfg["numero_equipos"],
        "competicion_numero_jornadas": cfg["numero_jornadas"],
        "competicion_formato": "liga regular ida y vuelta",
        "competicion_fecha_inicio": temporada_inicio,
        "competicion_fecha_fin": None,
        "temporada_etiqueta": temporada,
        "temporada_fecha_inicio": temporada_inicio,
        "temporada_fecha_fin": None,
        "jornada_numero": p.jornada,
        "fecha_hora_inicio": p.fecha_hora,
        "estado": "FINALIZADO",
        "goles_local": p.goles_local,
        "goles_visitante": p.goles_visitante,
        "local": {"nombre": p.local, "id_fuente": p.local_slug},
        "visitante": {"nombre": p.visitante, "id_fuente": p.visitante_slug},
        "estadio_nombre": None,
        "tipo_contenido_raw": "partido_laliga_historico_desde_jornada",
        "documento_url": url_jornada,
        "contenido_raw": raw,
        "obtenido_en": ahora_sql(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporada", required=True, help="Ej. 2022-23")
    parser.add_argument("--division", choices=sorted(DIVISIONES), required=True)
    parser.add_argument(
        "--jornadas",
        help="Opcional: 1,2,5-8. Por defecto, temporada completa.",
    )
    parser.add_argument("--pausa", type=float, default=0.35)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    validar_temporada(args.temporada)
    cfg = DIVISIONES[args.division]
    jornadas = jornadas_desde_texto(args.jornadas, cfg["numero_jornadas"])

    session = requests.Session()
    session.headers.update(HEADERS)

    cache_html: dict[int, str] = {}

    def descargar_jornada(j: int) -> tuple[str, str]:
        url = f"{BASE}/{cfg['slug']}/resultados/{args.temporada}/jornada-{j}"
        if j not in cache_html:
            cache_html[j] = get_con_reintentos(session, url).text
        return url, cache_html[j]

    # La fecha real de inicio se obtiene de J1; no se inventa un 1 de agosto.
    url_j1, html_j1 = descargar_jornada(1)
    partidos_j1 = extraer_partidos(html_j1, 1)
    if len(partidos_j1) != cfg["partidos_jornada"]:
        raise RuntimeError(
            f"J1 {args.temporada} {args.division}: esperaba "
            f"{cfg['partidos_jornada']} partidos y extraje {len(partidos_j1)}."
        )
    temporada_inicio = min(p.fecha_hora for p in partidos_j1)[:10]

    if args.dry_run:
        print(
            f"DRY-RUN {cfg['nombre']} {args.temporada}: "
            f"inicio={temporada_inicio}; jornadas={jornadas}"
        )
        for j in jornadas:
            url, html = descargar_jornada(j)
            partidos = extraer_partidos(html, j)
            print(f"J{j:02d}: {len(partidos)} partidos -> {url}")
            if len(partidos) != cfg["partidos_jornada"]:
                raise RuntimeError(
                    f"J{j}: esperaba {cfg['partidos_jornada']} y extraje {len(partidos)}."
                )
        return

    api = ApiIngesta()
    health = api.health()
    print("Puente IONOS OK ->", health.get("database"), health.get("db_version"))

    lote = api.iniciar_lote(
        fuente=FUENTE,
        tipo_fuente="scraping_historico",
        notas=(
            f"Backfill oficial {cfg['nombre']} {args.temporada}; "
            f"jornadas={jornadas}"
        ),
    )
    print("Lote abierto:", lote)

    creados = actualizados = errores = 0
    mensajes_error: list[str] = []

    for j in jornadas:
        url, html = descargar_jornada(j)
        print(f"\nJornada {j}: {url}")
        try:
            api.guardar_documento(
                {
                    "lote_id": lote,
                    "fuente": FUENTE,
                    "url": url,
                    "tipo_contenido": f"resultados_historicos_{cfg['slug']}",
                    "obtenido_en": ahora_sql(),
                    "contenido": html,
                }
            )

            partidos = extraer_partidos(html, j)
            print("  partidos extraídos:", len(partidos))
            if len(partidos) != cfg["partidos_jornada"]:
                raise RuntimeError(
                    f"Esperaba {cfg['partidos_jornada']} partidos y extraje {len(partidos)}."
                )

            for p in partidos:
                time.sleep(max(0.0, args.pausa))
                try:
                    res = api.guardar_partido(
                        payload_partido(
                            lote=lote,
                            temporada=args.temporada,
                            division=args.division,
                            cfg=cfg,
                            p=p,
                            url_jornada=url,
                            temporada_inicio=temporada_inicio,
                        )
                    )
                    if res.get("accion") == "creado":
                        creados += 1
                    else:
                        actualizados += 1
                    print(
                        f"  {p.local} {p.goles_local}-{p.goles_visitante} "
                        f"{p.visitante} -> {res.get('accion')} "
                        f"partido_id={res.get('partido_id')}"
                    )
                except Exception as exc:
                    errores += 1
                    mensajes_error.append(f"J{j} {p.local}-{p.visitante}: {exc}")
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
        f"\nResumen {cfg['nombre']} {args.temporada}: "
        f"creados={creados}, actualizados={actualizados}, errores={errores}"
    )
    if errores:
        raise RuntimeError(mensajes_error[0])


if __name__ == "__main__":
    main()
