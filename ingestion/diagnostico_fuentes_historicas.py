"""
Diagnóstico SIN escritura en BD para fuentes históricas Quiniela AI.

Objetivo v2:
- NO volver a probar Sofascore ni ESPN desde GitHub Actions.
- Verificar desde el MISMO runner tres fuentes complementarias:
  1) BDFutbol: histórico masculino + detalle de partido.
  2) SoccerDonna: Liga F y copas femeninas.
  3) Football-Data.co.uk: resultados + cuotas históricas SP1/SP2.

No usa INGEST_API_TOKEN y no escribe nada en MariaDB.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from dataclasses import dataclass
from typing import Iterable

import requests


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0 Safari/537.36"
)

session = requests.Session()
session.headers.update(
    {
        "User-Agent": UA,
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
    }
)


@dataclass
class Check:
    nombre: str
    ok: bool
    detalle: str


def get(url: str, timeout: int = 35) -> requests.Response:
    return session.get(url, timeout=timeout, allow_redirects=True)


def uniq(xs: Iterable[str]) -> list[str]:
    return sorted(set(xs))


def diagnostico_bdfutbol() -> list[Check]:
    print("\nBDFUTBOL — HISTÓRICO MASCULINO")
    print("-" * 64)

    checks: list[Check] = []
    competiciones = [
        (
            "LaLiga 2022-23",
            "https://www.bdfutbol.com/es/t/t2022-23.html",
            300,
        ),
        (
            "Segunda 2022-23",
            "https://www.bdfutbol.com/es/t/t2022-232a.html",
            400,
        ),
    ]

    sample_match_url: str | None = None

    for nombre, url, minimo_partidos in competiciones:
        try:
            r = get(url)
            text = r.text
            ids = uniq(
                re.findall(
                    r"(?:/es/)?p/p\.php\?id=(\d+)",
                    text,
                    flags=re.I,
                )
            )
            ok = r.status_code == 200 and len(ids) >= minimo_partidos
            detalle = (
                f"HTTP {r.status_code} | match_ids={len(ids)} "
                f"| bytes={len(r.content)}"
            )
            print(f"{nombre:<24} {detalle}")
            checks.append(Check(nombre, ok, detalle))
            if ids and sample_match_url is None:
                sample_match_url = (
                    "https://www.bdfutbol.com/es/p/p.php?id=" + ids[0]
                )
        except Exception as exc:
            detalle = f"ERROR {exc}"
            print(f"{nombre:<24} {detalle}")
            checks.append(Check(nombre, False, detalle))

    print("\nBDFutbol — detalle de partido")
    if sample_match_url is None:
        checks.append(Check("BDFutbol detalle", False, "sin partido de muestra"))
        print("sin partido de muestra")
        return checks

    try:
        r = get(sample_match_url)
        html = r.text
        low = html.casefold()

        # Señales de que la ficha trae la información que necesitamos.
        has_suplentes = "suplentes" in low
        has_entrenadores = "entrenadores" in low
        has_estadio = "estadio" in low
        has_fecha = "fecha" in low
        # BDFutbol enlaza jugadores desde la ficha. No dependemos del slug exacto:
        # contamos hrefs internos que parecen fichas de jugador/persona.
        playerish_links = uniq(
            re.findall(
                r'href=["\']([^"\']*(?:/j/|jugador)[^"\']*)["\']',
                html,
                flags=re.I,
            )
        )
        minute_tokens = len(
            re.findall(r"\b(?:[1-9]|[1-8]\d|90)(?:\+\d{1,2})?'", html)
        )

        ok = (
            r.status_code == 200
            and has_suplentes
            and has_entrenadores
            and has_estadio
            and has_fecha
        )
        detalle = (
            f"HTTP {r.status_code} | suplentes={has_suplentes} "
            f"| entrenadores={has_entrenadores} | estadio={has_estadio} "
            f"| fecha={has_fecha} | player_links={len(playerish_links)} "
            f"| minute_tokens={minute_tokens}"
        )
        print(sample_match_url)
        print(detalle)
        checks.append(Check("BDFutbol detalle", ok, detalle))
    except Exception as exc:
        detalle = f"ERROR {exc}"
        print(detalle)
        checks.append(Check("BDFutbol detalle", False, detalle))

    return checks


def diagnostico_soccerdonna() -> list[Check]:
    print("\nSOCCERDONNA — HISTÓRICO FEMENINO")
    print("-" * 64)

    checks: list[Check] = []

    # Liga F 2022-23: recorremos las 30 jornadas para comprobar cobertura real.
    all_match_ids: set[str] = set()
    ok_pages = 0
    errors: list[str] = []

    for jornada in range(1, 31):
        url = (
            "https://www.soccerdonna.de/en/primera-division-femenina/"
            "spieltagsuebersicht/wettbewerb_ESP1_2022_"
            f"{jornada}.html"
        )
        try:
            r = get(url)
            if r.status_code == 200:
                ok_pages += 1
                all_match_ids.update(
                    re.findall(r"spielbericht_(\d+)\.html", r.text, flags=re.I)
                )
            else:
                errors.append(f"J{jornada}:HTTP{r.status_code}")
        except Exception as exc:
            errors.append(f"J{jornada}:{type(exc).__name__}")

    # 16 equipos x 30 jornadas / 2 = 240 partidos teóricos.
    # Dejamos margen por aplazamientos/cambios de estructura del HTML.
    liga_ok = ok_pages >= 25 and len(all_match_ids) >= 180
    detalle = (
        f"paginas_200={ok_pages}/30 | match_report_ids={len(all_match_ids)}"
    )
    if errors:
        detalle += " | errores=" + ",".join(errors[:8])
    print(f"Liga F 2022-23           {detalle}")
    checks.append(Check("SoccerDonna Liga F", liga_ok, detalle))

    extras = [
        (
            "Copa de la Reina",
            "https://www.soccerdonna.de/de/copa-de-la-reina/"
            "gruppenspieltage/pokalwettbewerb_ESPP_2022.html",
            20,
        ),
        (
            "Supercopa femenina",
            "https://www.soccerdonna.de/de/supercopa-femenina/"
            "startseite/wettbewerb_ESPS_2022.html",
            3,
        ),
    ]

    for nombre, url, minimo in extras:
        try:
            r = get(url)
            ids = uniq(re.findall(r"spielbericht_(\d+)\.html", r.text, flags=re.I))
            ok = r.status_code == 200 and len(ids) >= minimo
            detalle = (
                f"HTTP {r.status_code} | match_report_ids={len(ids)} "
                f"| bytes={len(r.content)}"
            )
            print(f"{nombre:<24} {detalle}")
            checks.append(Check(f"SoccerDonna {nombre}", ok, detalle))
        except Exception as exc:
            detalle = f"ERROR {exc}"
            print(f"{nombre:<24} {detalle}")
            checks.append(Check(f"SoccerDonna {nombre}", False, detalle))

    return checks


def parse_football_data_csv(text: str) -> tuple[int, list[str]]:
    # utf-8-sig elimina BOM si existe.
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows = [row for row in reader if row and row.get("HomeTeam") and row.get("AwayTeam")]
    return len(rows), list(reader.fieldnames or [])


def diagnostico_football_data() -> list[Check]:
    print("\nFOOTBALL-DATA.CO.UK — RESULTADOS + CUOTAS")
    print("-" * 64)

    checks: list[Check] = []
    csvs = [
        (
            "SP1 2022-23",
            "https://www.football-data.co.uk/mmz4281/2223/SP1.csv",
            300,
        ),
        (
            "SP2 2022-23",
            "https://www.football-data.co.uk/mmz4281/2223/SP2.csv",
            400,
        ),
    ]

    for nombre, url, minimo in csvs:
        try:
            r = get(url)
            nrows = 0
            fields: list[str] = []
            if r.status_code == 200:
                # Requests suele detectar ISO-8859-1/Windows-1252. Si no, fallback.
                enc = r.encoding or "utf-8"
                try:
                    text = r.content.decode(enc)
                except Exception:
                    text = r.content.decode("latin-1")
                nrows, fields = parse_football_data_csv(text)

            required_core = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
            has_core = required_core.issubset(set(fields))

            # Detectar si existen cuotas 1X2 de al menos una casa/consenso.
            odds_triplets = [
                {"B365H", "B365D", "B365A"},
                {"PSH", "PSD", "PSA"},
                {"AvgH", "AvgD", "AvgA"},
                {"MaxH", "MaxD", "MaxA"},
            ]
            has_odds = any(t.issubset(set(fields)) for t in odds_triplets)

            ok = r.status_code == 200 and nrows >= minimo and has_core
            detalle = (
                f"HTTP {r.status_code} | partidos={nrows} "
                f"| core={has_core} | odds_1x2={has_odds} "
                f"| columnas={len(fields)} | bytes={len(r.content)}"
            )
            print(f"{nombre:<24} {detalle}")
            checks.append(Check(f"Football-data {nombre}", ok, detalle))
        except Exception as exc:
            detalle = f"ERROR {exc}"
            print(f"{nombre:<24} {detalle}")
            checks.append(Check(f"Football-data {nombre}", False, detalle))

    return checks


def main() -> None:
    print("DIAGNÓSTICO FUENTES HISTÓRICAS v2 — 2022-23")
    print("=" * 64)
    print("Este job NO escribe en la base de datos.")
    print("Sofascore y ESPN quedan fuera de este diagnóstico por bloqueo 403.\n")

    checks: list[Check] = []
    checks.extend(diagnostico_bdfutbol())
    checks.extend(diagnostico_soccerdonna())
    checks.extend(diagnostico_football_data())

    print("\nRESUMEN")
    print("-" * 64)
    for c in checks:
        print(f"{'OK' if c.ok else 'FAIL':<5} {c.nombre}: {c.detalle}")

    # Criterio de arquitectura mínima:
    # - BDFutbol debe cubrir Primera + Segunda + detalle.
    # - SoccerDonna debe cubrir Liga F.
    # - Football-data es muy recomendable (cuotas), pero no bloquea por sí solo
    #   el histórico deportivo si BDFutbol funciona.
    required_names = {
        "LaLiga 2022-23",
        "Segunda 2022-23",
        "BDFutbol detalle",
        "SoccerDonna Liga F",
    }
    required = [c for c in checks if c.nombre in required_names]
    missing = [c.nombre for c in required if not c.ok]

    if missing:
        print("\nCHECK ROJO: faltan fuentes base:", ", ".join(missing))
        raise RuntimeError(
            "El mosaico histórico todavía no cubre las tres ligas base."
        )

    fd_ok = any(c.ok for c in checks if c.nombre.startswith("Football-data"))
    print("\nCHECK VERDE: BDFutbol + SoccerDonna cubren las ligas base.")
    if fd_ok:
        print("CHECK VERDE EXTRA: Football-data está accesible para resultados/cuotas.")
    else:
        print(
            "AVISO: Football-data no pasó desde el runner; "
            "no impide construir el histórico deportivo, pero habrá que resolver cuotas aparte."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nERROR FINAL: {exc}", file=sys.stderr)
        raise
