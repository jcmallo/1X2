"""
Diagnóstico SIN escritura en BD para elegir la fuente histórica definitiva.

Motivo:
- Sofascore devuelve 403 desde GitHub Actions.
- El proxy IONOS también recibe bloqueo upstream.
- Antes de reprogramar el importador completo, probamos desde EL MISMO
  GitHub runner si ESPN cubre 2022-23 y qué detalle de jugadores devuelve.

No usa INGEST_API_TOKEN y no escribe nada en MariaDB.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from typing import Any

import requests


DATE_RANGE = "20220801-20230630"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

LEAGUES = [
    ("esp.1", "LaLiga", "M"),
    ("esp.2", "Segunda División", "M"),
    ("esp.w.1", "Liga F", "F"),
    ("esp.copa_del_rey", "Copa del Rey", "M"),
    ("esp.super_cup", "Supercopa de España", "M"),
    ("esp.copa_de_la_reina", "Copa de la Reina", "F"),
    ("uefa.champions", "Champions League", "M"),
    ("uefa.champions_qual", "Champions League Qualifying", "M"),
    ("uefa.europa", "Europa League", "M"),
    ("uefa.europa_qual", "Europa League Qualifying", "M"),
    ("uefa.europa.conf", "Conference League", "M"),
    ("uefa.europa.conf_qual", "Conference League Qualifying", "M"),
    ("uefa.super_cup", "UEFA Super Cup", "M"),
    ("uefa.wchampions", "Women's Champions League", "F"),
    ("fifa.cwc", "FIFA Club World Cup", "M"),
    ("fifa.intercontinental_cup", "FIFA Intercontinental Cup", "M"),
]

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
)


def get_json(url: str, timeout: int = 35) -> tuple[int, dict | None]:
    r = session.get(url, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = None
    return r.status_code, data if isinstance(data, dict) else None


def event_team_ids(event: dict) -> list[str]:
    salida = []
    comps = event.get("competitions")
    if not isinstance(comps, list) or not comps:
        return salida
    competitors = comps[0].get("competitors")
    if not isinstance(competitors, list):
        return salida
    for c in competitors:
        if not isinstance(c, dict):
            continue
        team = c.get("team")
        if isinstance(team, dict) and team.get("id") is not None:
            salida.append(str(team["id"]))
    return salida


def sample_summary(slug: str, event_id: str) -> dict:
    url = f"{ESPN_BASE}/{slug}/summary?event={event_id}"
    status, data = get_json(url)
    if status != 200 or not data:
        return {
            "status": status,
            "error": "summary no disponible",
        }

    result: dict[str, Any] = {
        "status": status,
        "top_keys": sorted(data.keys()),
        "boxscore_players_sections": 0,
        "athletes_in_boxscore": 0,
        "stat_labels": [],
        "has_rosters": isinstance(data.get("rosters"), list),
        "roster_entries": 0,
        "has_keyEvents": isinstance(data.get("keyEvents"), list),
    }

    boxscore = data.get("boxscore")
    if isinstance(boxscore, dict):
        players = boxscore.get("players")
        if isinstance(players, list):
            result["boxscore_players_sections"] = len(players)
            labels = set()
            athletes = 0
            for team_section in players:
                if not isinstance(team_section, dict):
                    continue
                stats_groups = team_section.get("statistics")
                if not isinstance(stats_groups, list):
                    continue
                for group in stats_groups:
                    if not isinstance(group, dict):
                        continue
                    group_labels = group.get("labels")
                    if isinstance(group_labels, list):
                        for x in group_labels:
                            if x is not None:
                                labels.add(str(x))
                    aa = group.get("athletes")
                    if isinstance(aa, list):
                        athletes += len(aa)
            result["athletes_in_boxscore"] = athletes
            result["stat_labels"] = sorted(labels)

    rosters = data.get("rosters")
    if isinstance(rosters, list):
        total = 0
        for r in rosters:
            if not isinstance(r, dict):
                continue
            rr = r.get("roster")
            if isinstance(rr, list):
                total += len(rr)
        result["roster_entries"] = total

    # RAW sample shape, never full payload in log.
    result["has_minute_label"] = any(
        str(x).upper() in {"MIN", "MINS", "MINUTES"}
        or "MIN" == str(x).upper().strip()
        for x in result["stat_labels"]
    )

    return result


def test_soccerdonna() -> dict:
    urls = [
        (
            "Supercopa femenina",
            "https://www.soccerdonna.de/de/supercopa-femenina/"
            "startseite/wettbewerb_ESPS_2022.html",
        ),
        (
            "Copa de la Reina",
            "https://www.soccerdonna.de/de/copa-de-la-reina/"
            "gruppenspieltage/pokalwettbewerb_ESPP_2022.html",
        ),
    ]
    out = {}
    for name, url in urls:
        try:
            r = session.get(url, timeout=35)
            links = len(
                set(re.findall(r"spielbericht_(\d+)\.html", r.text))
            )
            out[name] = {
                "status": r.status_code,
                "match_report_ids": links,
                "bytes": len(r.content),
            }
        except Exception as exc:
            out[name] = {"error": str(exc)}
    return out


def main() -> None:
    print("DIAGNÓSTICO FUENTES HISTÓRICAS 2022-23")
    print("=" * 52)
    print("Este job NO escribe en la base de datos.\n")

    events_by_slug: dict[str, list[dict]] = {}
    failures = []

    for slug, nombre, genero in LEAGUES:
        url = (
            f"{ESPN_BASE}/{slug}/scoreboard"
            f"?dates={DATE_RANGE}&limit=1000"
        )
        try:
            status, data = get_json(url)
            if status != 200 or not data:
                print(f"[ESPN] {nombre:<31} HTTP {status}")
                # Un 404/400 de una competición opcional no invalida ESPN entero.
                continue

            events = data.get("events")
            if not isinstance(events, list):
                events = []

            events_by_slug[slug] = [
                x for x in events if isinstance(x, dict)
            ]

            teams = set()
            for ev in events_by_slug[slug]:
                teams.update(event_team_ids(ev))

            print(
                f"[ESPN] {nombre:<31} "
                f"HTTP 200 | partidos={len(events_by_slug[slug]):>3} "
                f"| equipos={len(teams):>2}"
            )
        except Exception as exc:
            failures.append(f"{slug}: {exc}")
            print(f"[ESPN] {nombre:<31} ERROR {exc}")

    print("\nDETALLE DE PARTIDO / JUGADORES")
    print("-" * 52)

    for slug in ("esp.1", "esp.2", "esp.w.1", "uefa.champions", "uefa.wchampions"):
        events = events_by_slug.get(slug) or []
        if not events:
            print(f"{slug}: sin evento de muestra")
            continue

        # Preferir evento finalizado.
        chosen = None
        for ev in events:
            status = ev.get("status")
            if isinstance(status, dict):
                t = status.get("type")
                if isinstance(t, dict) and t.get("completed") is True:
                    chosen = ev
                    break
        chosen = chosen or events[0]

        eid = str(chosen.get("id"))
        detail = sample_summary(slug, eid)
        print(
            f"{slug} event={eid}: "
            f"summary_http={detail.get('status')} | "
            f"players={detail.get('athletes_in_boxscore', 0)} | "
            f"roster={detail.get('roster_entries', 0)} | "
            f"MIN={detail.get('has_minute_label', False)}"
        )
        labels = detail.get("stat_labels") or []
        if labels:
            print("  labels:", ", ".join(labels[:35]))

    print("\nSUPLEMENTO FEMENINO (SoccerDonna)")
    print("-" * 52)
    donna = test_soccerdonna()
    for name, info in donna.items():
        print(f"{name}: {json.dumps(info, ensure_ascii=False)}")

    # Criterio mínimo: ESPN debe devolver las tres ligas base.
    required = ("esp.1", "esp.2", "esp.w.1")
    missing = [
        slug for slug in required
        if not events_by_slug.get(slug)
    ]

    print("\nRESUMEN")
    print("-" * 52)
    print("ligas_base_ok:", 3 - len(missing), "/3")
    print("errores_red:", len(failures))
    if missing:
        print("faltan:", ", ".join(missing))
        raise RuntimeError(
            "ESPN no cubrió las 3 ligas base desde GitHub Actions."
        )

    print(
        "CHECK VERDE: ESPN es accesible desde GitHub Actions "
        "y cubre las tres ligas base 2022-23."
    )


if __name__ == "__main__":
    main()
