"""
Carga histórica por temporada para Quiniela AI.

Objetivo:
- 5 temporadas independientes, una ejecución por temporada.
- Descubre los equipos de Primera, Segunda y Liga F de ESA temporada.
- A partir de esos equipos recorre sus partidos y captura TODAS sus
  competiciones oficiales (Liga, Copa, Supercopas, UEFA, FIFA, etc.).
- Excluye amistosos.
- Guarda partidos, participantes, resultados, alineaciones, minutos y
  estadísticas disponibles.
- Idempotente y reanudable: si una ejecución falla, se vuelve a lanzar
  la misma temporada sin crear duplicados.

Fuente de cobertura transversal: Sofascore.
Las fuentes oficiales ya existentes (laliga.com / ligaf.es) se conservan;
guardar_partido_historico.php reconcilia contra partidos ya cargados.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from api_client import ApiIngesta


SOFASCORE_DIRECTOS = [
    "https://api.sofascore.com/api/v1",
    "https://www.sofascore.com/api/v1",
]
SOFASCORE_CANONICO = "https://www.sofascore.com/api/v1"

BASE_TOURNAMENTS = {
    "MASCULINO": [
        {"id": 8, "nombre": "LaLiga"},
        {"id": 54, "nombre": "Segunda División"},
    ],
    "FEMENINO": [
        {"id": 1127, "nombre": "Liga F"},
    ],
}

NOMBRES_CANONICOS = {
    8: ("LaLiga", "España", "PRIMERA_CATEGORIA", "LaLiga", 1),
    54: ("Segunda División", "España", "SEGUNDA_CATEGORIA", "LaLiga", 1),
    1127: ("Liga F", "España", "PRIMERA_CATEGORIA", "Liga F", 1),
    329: ("Copa del Rey", "España", "COPA", "RFEF", 1),
    213: ("Supercopa de España", "España", "SUPERCOPA", "RFEF", 0),
    7: ("UEFA Champions League", "Europa", "CLUB_INTERNACIONAL", "UEFA", 0),
    679: ("UEFA Europa League", "Europa", "CLUB_INTERNACIONAL", "UEFA", 0),
    17015: ("UEFA Conference League", "Europa", "CLUB_INTERNACIONAL", "UEFA", 0),
    465: ("UEFA Super Cup", "Europa", "CLUB_INTERNACIONAL", "UEFA", 0),
    357: ("FIFA Club World Cup", "Mundo", "CLUB_INTERNACIONAL", "FIFA", 0),
    23674: ("FIFA Intercontinental Cup", "Mundo", "CLUB_INTERNACIONAL", "FIFA", 0),
    11126: ("Copa de la Reina", "España", "COPA", "RFEF", 0),
    14687: ("Supercopa Femenina", "España", "SUPERCOPA", "RFEF", 0),
    696: ("UEFA Women's Champions League", "Europa", "CLUB_INTERNACIONAL", "UEFA", 0),
    29194: ("UEFA Women's Europa Cup", "Europa", "CLUB_INTERNACIONAL", "UEFA", 0),
    29871: ("Women's Champions Cup", "Mundo", "CLUB_INTERNACIONAL", "FIFA", 0),
}

POSICIONES = {
    "G": "Portero",
    "D": "Defensa",
    "M": "Centrocampista",
    "F": "Delantero",
    "GK": "Portero",
    "DEF": "Defensa",
    "MID": "Centrocampista",
    "FWD": "Delantero",
}


class Sofascore:
    """
    GitHub-hosted runners pueden recibir 403 de Sofascore aunque el endpoint
    sea público. Estrategia:
      1. probar api.sofascore.com y www.sofascore.com directamente;
      2. ante 403/401/bloqueo de red, usar el proxy autenticado de IONOS.

    Así no dependemos de la reputación/IP del runner de GitHub.
    """

    def __init__(self, api: ApiIngesta) -> None:
        self.api = api
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Referer": "https://www.sofascore.com/",
                "Origin": "https://www.sofascore.com",
            }
        )
        self.usar_proxy = False
        self._proxy_anunciado = False

    def _proxy(self, path: str, *, aceptar_404: bool) -> dict | None:
        if not self._proxy_anunciado:
            print(
                "      Sofascore directo bloqueado; "
                "continuando mediante IONOS..."
            )
            self._proxy_anunciado = True

        data = self.api._request_json(
            "GET",
            "sofascore_proxy.php",
            params={"path": path},
            timeout=60,
        )

        status = int(data.get("upstream_status") or 0)
        if status == 404 and aceptar_404:
            return None
        if status != 200:
            raise RuntimeError(
                f"Proxy Sofascore devolvió upstream_status={status}"
            )

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise RuntimeError("Proxy Sofascore devolvió JSON inválido.")

        return payload

    def get(self, path: str, *, aceptar_404: bool = False) -> dict | None:
        if self.usar_proxy:
            return self._proxy(path, aceptar_404=aceptar_404)

        ultimo: Exception | None = None
        bloqueado = False

        for base in SOFASCORE_DIRECTOS:
            url = path if path.startswith("http") else base + path

            try:
                r = self.s.get(url, timeout=(10, 35))

                if r.status_code == 404 and aceptar_404:
                    return None

                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        return data
                    raise RuntimeError("JSON no es un objeto.")

                # No perder 1-2 minutos reintentando un 403 del runner:
                # cambiar inmediatamente de ruta de red.
                if r.status_code in {401, 403}:
                    bloqueado = True
                    ultimo = RuntimeError(
                        f"HTTP {r.status_code} en {base}"
                    )
                    continue

                if r.status_code in {429, 500, 502, 503, 504}:
                    ultimo = RuntimeError(
                        f"HTTP {r.status_code} en {base}"
                    )
                    continue

                raise RuntimeError(
                    f"Sofascore HTTP {r.status_code}: {r.text[:200]}"
                )

            except (
                requests.Timeout,
                requests.ConnectionError,
                requests.JSONDecodeError,
            ) as exc:
                ultimo = exc
                continue

        # Si los dos hosts directos fallan, IONOS es el camino estable.
        self.usar_proxy = True
        return self._proxy(path, aceptar_404=aceptar_404)


def parse_temporada(etiqueta: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", etiqueta)
    if not m:
        raise ValueError("Temporada debe tener formato 2022-23.")

    inicio = int(m.group(1))
    fin2 = int(m.group(2))
    siglo = (inicio // 100) * 100
    fin = siglo + fin2
    if fin < inicio:
        fin += 100

    if fin != inicio + 1:
        raise ValueError("Temporada no consecutiva.")

    return inicio, fin


def temporada_variantes(etiqueta: str) -> set[str]:
    inicio, fin = parse_temporada(etiqueta)
    corto_i = str(inicio)[2:]
    corto_f = str(fin)[2:]
    return {
        etiqueta.lower(),
        f"{inicio}/{fin}".lower(),
        f"{inicio}-{fin}".lower(),
        f"{corto_i}/{corto_f}".lower(),
        f"{corto_i}-{corto_f}".lower(),
    }


def season_score(season: dict, etiqueta: str) -> int:
    variantes = temporada_variantes(etiqueta)
    inicio, fin = parse_temporada(etiqueta)

    texto = " ".join(
        str(season.get(k) or "")
        for k in ("name", "year")
    ).lower()

    score = 0
    for v in variantes:
        if v and v in texto:
            score = max(score, 100)

    # Algunos torneos (Supercopas/FIFA) usan solo el año natural.
    nums = {int(x) for x in re.findall(r"\b20\d{2}\b", texto)}
    if inicio in nums:
        score = max(score, 60)
    if fin in nums:
        score = max(score, 55)

    return score


def descubrir_season_id(
    sf: Sofascore,
    torneo_id: int,
    temporada: str,
) -> int:
    data = sf.get(f"/unique-tournament/{torneo_id}/seasons")
    assert data is not None

    seasons = data.get("seasons")
    if not isinstance(seasons, list):
        raise RuntimeError(
            f"unique-tournament {torneo_id} no devolvió seasons."
        )

    candidatos = []
    for s in seasons:
        if not isinstance(s, dict) or s.get("id") is None:
            continue
        candidatos.append((season_score(s, temporada), s))

    candidatos.sort(key=lambda x: x[0], reverse=True)

    if not candidatos or candidatos[0][0] < 80:
        raise RuntimeError(
            f"No encuentro temporada {temporada} "
            f"en unique-tournament {torneo_id}."
        )

    return int(candidatos[0][1]["id"])


def ts(evento: dict) -> int | None:
    v = evento.get("startTimestamp")
    return int(v) if isinstance(v, (int, float)) else None


def dentro_ventana(evento: dict, inicio_ts: int, fin_ts: int) -> bool:
    t = ts(evento)
    return t is not None and inicio_ts <= t <= fin_ts


def eventos_torneo_temporada(
    sf: Sofascore,
    torneo_id: int,
    season_id: int,
) -> list[dict]:
    salida: dict[int, dict] = {}

    for modo in ("last", "next"):
        for pagina in range(0, 100):
            data = sf.get(
                f"/unique-tournament/{torneo_id}/season/"
                f"{season_id}/events/{modo}/{pagina}",
                aceptar_404=True,
            )

            if not data:
                break

            events = data.get("events")
            if not isinstance(events, list) or not events:
                break

            for e in events:
                if isinstance(e, dict) and isinstance(e.get("id"), int):
                    salida[int(e["id"])] = e

            if data.get("hasNextPage") is not True:
                break

            time.sleep(0.05)

    return list(salida.values())


def eventos_equipo_ventana(
    sf: Sofascore,
    equipo_id: int,
    inicio_ts: int,
    fin_ts: int,
) -> list[dict]:
    salida: dict[int, dict] = {}

    # Históricos.
    for pagina in range(0, 80):
        data = sf.get(
            f"/team/{equipo_id}/events/last/{pagina}",
            aceptar_404=True,
        )
        if not data:
            break

        events = data.get("events")
        if not isinstance(events, list) or not events:
            break

        timestamps = []

        for e in events:
            if not isinstance(e, dict) or not isinstance(e.get("id"), int):
                continue
            t = ts(e)
            if t is not None:
                timestamps.append(t)
            if dentro_ventana(e, inicio_ts, fin_ts):
                salida[int(e["id"])] = e

        if timestamps and min(timestamps) < inicio_ts:
            break
        if data.get("hasNextPage") is not True:
            break

        time.sleep(0.04)

    # Próximos, especialmente útil para la temporada actual.
    for pagina in range(0, 30):
        data = sf.get(
            f"/team/{equipo_id}/events/next/{pagina}",
            aceptar_404=True,
        )
        if not data:
            break

        events = data.get("events")
        if not isinstance(events, list) or not events:
            break

        timestamps = []

        for e in events:
            if not isinstance(e, dict) or not isinstance(e.get("id"), int):
                continue
            t = ts(e)
            if t is not None:
                timestamps.append(t)
            if dentro_ventana(e, inicio_ts, fin_ts):
                salida[int(e["id"])] = e

        if timestamps and max(timestamps) > fin_ts:
            break
        if data.get("hasNextPage") is not True:
            break

        time.sleep(0.04)

    return list(salida.values())


def es_amistoso(evento: dict) -> bool:
    tournament = evento.get("tournament")
    if not isinstance(tournament, dict):
        return True

    unique = tournament.get("uniqueTournament")
    textos = [
        tournament.get("name"),
    ]
    if isinstance(unique, dict):
        textos.append(unique.get("name"))

    t = " ".join(str(x or "") for x in textos).casefold()
    patrones = (
        "friendly",
        "amistoso",
        "club friendly",
        "club friendlies",
    )
    return any(p in t for p in patrones)


def equipo_ids_evento(evento: dict) -> tuple[int | None, int | None]:
    home = evento.get("homeTeam")
    away = evento.get("awayTeam")

    h = home.get("id") if isinstance(home, dict) else None
    a = away.get("id") if isinstance(away, dict) else None

    return (
        int(h) if isinstance(h, int) else None,
        int(a) if isinstance(a, int) else None,
    )


def info_competicion(
    evento: dict,
    genero: str,
) -> tuple[str, str, str, str, int, int | None]:
    tournament = evento.get("tournament")
    tournament = tournament if isinstance(tournament, dict) else {}

    unique = tournament.get("uniqueTournament")
    unique = unique if isinstance(unique, dict) else {}

    tid = unique.get("id")
    tid = int(tid) if isinstance(tid, int) else None

    if tid in NOMBRES_CANONICOS:
        nombre, pais, nivel, organizador, apta = NOMBRES_CANONICOS[tid]
        return nombre, pais, nivel, organizador, apta, tid

    nombre = (
        str(unique.get("name") or tournament.get("name") or "Competición")
        .strip()
    )[:150]

    category = unique.get("category")
    if not isinstance(category, dict):
        category = tournament.get("category")
    category = category if isinstance(category, dict) else {}

    pais = str(category.get("name") or "Mundo")[:80]

    n = nombre.casefold()
    if "supercopa" in n or "super cup" in n:
        nivel = "SUPERCOPA"
    elif "copa" in n or "cup" in n:
        if pais.casefold() in {"europe", "europa", "world", "mundo"}:
            nivel = "CLUB_INTERNACIONAL"
        else:
            nivel = "COPA"
    elif pais.casefold() in {"europe", "europa", "world", "mundo"}:
        nivel = "CLUB_INTERNACIONAL"
    else:
        nivel = "OTRO"

    if "uefa" in n:
        organizador = "UEFA"
    elif "fifa" in n:
        organizador = "FIFA"
    elif pais.casefold() in {"spain", "espana", "españa"}:
        organizador = "RFEF"
    else:
        organizador = "Desconocido"

    apta = 1 if nivel in {
        "PRIMERA_CATEGORIA",
        "SEGUNDA_CATEGORIA",
        "COPA",
    } and genero == "MASCULINO" and pais.casefold() in {
        "spain",
        "espana",
        "españa",
    } else 0

    return nombre, pais, nivel, organizador, apta, tid


def fase(evento: dict, nivel: str, torneo_id: int | None) -> tuple[str, bool, int | None]:
    round_info = evento.get("roundInfo")
    round_info = round_info if isinstance(round_info, dict) else {}

    if torneo_id in {8, 54, 1127}:
        return "Liga regular", False, 2

    nombre = str(
        round_info.get("name")
        or round_info.get("slug")
        or "Competición"
    ).strip()

    if nombre == "Competición":
        r = round_info.get("round")
        if isinstance(r, int):
            nombre = f"Ronda {r}"

    eliminatoria = nivel in {
        "COPA",
        "SUPERCOPA",
    }
    if any(
        x in nombre.casefold()
        for x in (
            "final",
            "semi",
            "quarter",
            "octavos",
            "cuartos",
            "dieciseis",
            "round of",
            "qualif",
            "playoff",
        )
    ):
        eliminatoria = True

    return nombre[:100], eliminatoria, None


def map_estado(evento: dict) -> str:
    status = evento.get("status")
    status = status if isinstance(status, dict) else {}
    tipo = str(status.get("type") or "").casefold()

    if tipo in {"finished", "afterextra", "afterpenalties"}:
        return "FINALIZADO"
    if tipo in {"postponed"}:
        return "APLAZADO"
    if tipo in {"canceled", "cancelled"}:
        return "CANCELADO"
    if tipo in {"suspended", "interrupted"}:
        return "SUSPENDIDO"
    if tipo in {"inprogress", "live"}:
        return "EN_JUEGO"
    if tipo in {"walkover"}:
        return "ADJUDICADO"
    return "PROGRAMADO"


def score_val(score: Any, campo: str) -> int | None:
    if not isinstance(score, dict):
        return None
    v = score.get(campo)
    if isinstance(v, (int, float)):
        return int(v)
    return None


def nombre_estadio(evento: dict) -> str | None:
    venue = evento.get("venue")
    if not isinstance(venue, dict):
        return None

    stadium = venue.get("stadium")
    if isinstance(stadium, dict) and stadium.get("name"):
        return str(stadium["name"])[:150]

    if venue.get("name"):
        return str(venue["name"])[:150]

    return None


def jornada(evento: dict) -> int | None:
    r = evento.get("roundInfo")
    if not isinstance(r, dict):
        return None
    v = r.get("round")
    return int(v) if isinstance(v, int) else None


def iso_local_madrid(timestamp: int) -> str:
    # Para no depender de tzdata de terceros usamos UTC y convertimos con zoneinfo.
    from zoneinfo import ZoneInfo

    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .astimezone(ZoneInfo("Europe/Madrid"))
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def fecha_local_madrid(timestamp: int) -> str:
    return iso_local_madrid(timestamp)[:10]


def jugador_payload(
    item: dict,
    equipo_id: int,
) -> dict | None:
    player = item.get("player")
    if not isinstance(player, dict) or not isinstance(player.get("id"), int):
        return None

    nombre = str(
        player.get("name")
        or player.get("shortName")
        or ""
    ).strip()
    if not nombre:
        return None

    stats = item.get("statistics")
    stats = stats if isinstance(stats, dict) else {}

    pos = player.get("position")
    pos_txt = POSICIONES.get(str(pos).upper(), str(pos) if pos else None)

    country = player.get("country")
    nacionalidad = None
    if isinstance(country, dict):
        nacionalidad = country.get("name") or country.get("alpha2")

    altura = player.get("height")
    if not isinstance(altura, (int, float)):
        altura = None

    fecha_nacimiento = None
    dob_ts = player.get("dateOfBirthTimestamp")
    if isinstance(dob_ts, (int, float)):
        fecha_nacimiento = datetime.fromtimestamp(
            int(dob_ts), tz=timezone.utc
        ).strftime("%Y-%m-%d")

    dorsal = item.get("jerseyNumber")
    if dorsal is None:
        dorsal = player.get("jerseyNumber")
    try:
        dorsal = int(dorsal) if dorsal not in (None, "") else None
    except (TypeError, ValueError):
        dorsal = None

    substitute = item.get("substitute")
    titular = 0 if substitute is True else 1

    def g(*keys: str):
        for k in keys:
            if stats.get(k) is not None:
                return stats.get(k)
        return None

    aereos = g("aerialWon")
    return {
        "id_fuente": str(player["id"]),
        "equipo_id_fuente": str(equipo_id),
        "nombre": nombre[:150],
        "fecha_nacimiento": fecha_nacimiento,
        "nacionalidad": str(nacionalidad)[:80] if nacionalidad else None,
        "altura_cm": int(altura) if isinstance(altura, (int, float)) else None,
        "posicion": pos_txt[:30] if isinstance(pos_txt, str) else None,
        "dorsal": dorsal,
        "es_titular": titular,
        "minutos": g("minutesPlayed"),
        "valoracion": g("rating"),
        "goles": g("goals"),
        "asistencias": g("goalAssist", "assists"),
        "tiros": g("totalShots"),
        "tiros_a_puerta": g("onTargetScoringAttempt"),
        "xg": g("expectedGoals"),
        "xa": g("expectedAssists"),
        "pases": g("totalPass"),
        "pases_completados": g("accuratePass"),
        "pases_clave": g("keyPass"),
        "regates": g("totalContest"),
        "regates_exitosos": g("wonContest"),
        "duelos": g("totalDuels"),
        "duelos_ganados": g("duelWon"),
        "duelos_aereos": aereos,
        "entradas": g("totalTackle"),
        "intercepciones": g("interceptionWon"),
        "despejes": g("totalClearance"),
        "bloqueos": g("outfielderBlock"),
        "faltas_cometidas": g("fouls"),
        "faltas_recibidas": g("wasFouled"),
        "tarjetas_amarillas": g("yellowCards"),
        "tarjetas_rojas": g("redCards"),
        "fueras_de_juego": g("offsides"),
        "paradas": g("saves"),
    }


def parse_lineups(
    data: dict | None,
    home_id: int,
    away_id: int,
) -> list[dict]:
    if not data:
        return []

    salida = []

    for lado, team_id in (("home", home_id), ("away", away_id)):
        bloque = data.get(lado)
        if not isinstance(bloque, dict):
            continue
        players = bloque.get("players")
        if not isinstance(players, list):
            continue

        for item in players:
            if not isinstance(item, dict):
                continue
            p = jugador_payload(item, team_id)
            if p:
                salida.append(p)

    return salida


def num_stat(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:[.,]\d+)?", v)
        if m:
            return float(m.group(0).replace(",", "."))
    return None


def parse_team_statistics(
    data: dict | None,
    home_id: int,
    away_id: int,
) -> list[dict]:
    base = {
        "home": {"equipo_id_fuente": str(home_id), "es_local": 1},
        "away": {"equipo_id_fuente": str(away_id), "es_local": 0},
    }

    if not data:
        return list(base.values())

    periodos = data.get("statistics")
    if not isinstance(periodos, list):
        return list(base.values())

    periodo_all = None
    for p in periodos:
        if isinstance(p, dict) and str(p.get("period")).upper() == "ALL":
            periodo_all = p
            break
    if periodo_all is None and periodos:
        periodo_all = periodos[0] if isinstance(periodos[0], dict) else None

    if not isinstance(periodo_all, dict):
        return list(base.values())

    grupos = periodo_all.get("groups")
    if not isinstance(grupos, list):
        return list(base.values())

    mapping = {
        "ball possession": "posesion_pct",
        "total shots": "tiros",
        "shots on target": "tiros_a_puerta",
        "expected goals": "xg",
        "corner kicks": "corners",
        "fouls": "faltas",
        "yellow cards": "tarjetas_amarillas",
        "red cards": "tarjetas_rojas",
        "offsides": "fueras_de_juego",
        "total passes": "pases",
        "passes": "pases",
    }

    for g in grupos:
        if not isinstance(g, dict):
            continue
        items = g.get("statisticsItems")
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            nombre = str(item.get("name") or "").casefold().strip()
            campo = mapping.get(nombre)
            if not campo:
                continue

            hv = num_stat(item.get("home"))
            av = num_stat(item.get("away"))

            if hv is not None:
                base["home"][campo] = hv
            if av is not None:
                base["away"][campo] = av

            if nombre == "ball possession":
                pass

            # Accurate passes a veces trae "410 (88%)".
            if "accurate passes" in nombre:
                pass

    # Buscar específicamente accurate passes para el porcentaje.
    for g in grupos:
        if not isinstance(g, dict):
            continue
        items = g.get("statisticsItems")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            nombre = str(item.get("name") or "").casefold().strip()
            if "accurate passes" not in nombre:
                continue
            for lado in ("home", "away"):
                valor = item.get(lado)
                if isinstance(valor, str):
                    m = re.search(r"\((\d+(?:[.,]\d+)?)%\)", valor)
                    if m:
                        base[lado]["precision_pases_pct"] = float(
                            m.group(1).replace(",", ".")
                        )

    return list(base.values())


def detalle_evento(
    sf: Sofascore,
    event_id: int,
    home_id: int,
    away_id: int,
) -> tuple[dict | None, dict | None, list[dict], list[dict]]:
    lineups_url = f"/event/{event_id}/lineups"
    stats_url = f"/event/{event_id}/statistics"

    lineups = sf.get(lineups_url, aceptar_404=True)
    statistics = sf.get(stats_url, aceptar_404=True)

    jugadores = parse_lineups(lineups, home_id, away_id)
    equipos = parse_team_statistics(statistics, home_id, away_id)

    return lineups, statistics, jugadores, equipos


def payload_partido(
    evento: dict,
    temporada: str,
    genero: str,
    inicio_global: str,
    fin_global: str,
) -> dict:
    event_id = int(evento["id"])
    home = evento.get("homeTeam")
    away = evento.get("awayTeam")
    if not isinstance(home, dict) or not isinstance(away, dict):
        raise RuntimeError(f"Evento {event_id} sin equipos.")

    home_id = int(home["id"])
    away_id = int(away["id"])

    nombre_comp, pais, nivel, organizador, apta, torneo_id = info_competicion(
        evento, genero
    )
    fase_nombre, eliminatoria, ida_vuelta = fase(
        evento, nivel, torneo_id
    )

    home_score = evento.get("homeScore")
    away_score = evento.get("awayScore")

    hubo_prorroga = 1 if (
        isinstance(home_score, dict) and home_score.get("overtime") is not None
    ) or (
        isinstance(away_score, dict) and away_score.get("overtime") is not None
    ) else 0

    hubo_penaltis = 1 if (
        isinstance(home_score, dict) and home_score.get("penalties") is not None
    ) or (
        isinstance(away_score, dict) and away_score.get("penalties") is not None
    ) else 0

    timestamp = ts(evento)
    if timestamp is None:
        raise RuntimeError(f"Evento {event_id} sin startTimestamp.")

    return {
        "lote_id": 0,  # se rellena fuera
        "fuente": "sofascore",
        "id_partido_fuente": str(event_id),
        "temporada_etiqueta": temporada,
        "temporada_fecha_inicio": inicio_global,
        "temporada_fecha_fin": fin_global,
        "competicion_nombre": nombre_comp,
        "competicion_pais": pais,
        "competicion_genero": genero,
        "competicion_nivel": nivel,
        "competicion_organizador": organizador,
        "competicion_apta_quiniela": apta,
        "competicion_fecha_inicio": inicio_global,
        "competicion_fecha_fin": fin_global,
        "fase_nombre": fase_nombre,
        "fase_es_eliminatoria": 1 if eliminatoria else 0,
        "fase_numero_ida_vuelta": ida_vuelta,
        "jornada_numero": jornada(evento),
        "local": {
            "id_fuente": str(home_id),
            "nombre": str(home.get("name") or home.get("shortName") or home_id)[:150],
        },
        "visitante": {
            "id_fuente": str(away_id),
            "nombre": str(away.get("name") or away.get("shortName") or away_id)[:150],
        },
        "fecha_hora_inicio": iso_local_madrid(timestamp),
        "estado": map_estado(evento),
        "goles_local": score_val(home_score, "current"),
        "goles_visitante": score_val(away_score, "current"),
        "goles_local_descanso": score_val(home_score, "period1"),
        "goles_visitante_descanso": score_val(away_score, "period1"),
        "hubo_prorroga": hubo_prorroga,
        "hubo_penaltis": hubo_penaltis,
        "estadio_nombre": nombre_estadio(evento),
        "documento_url": f"{SOFASCORE_CANONICO}/event/{event_id}",
        "contenido_raw": json.dumps(
            evento,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "obtenido_en": datetime.now(timezone.utc)
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporada", required=True)
    args = parser.parse_args()

    temporada = args.temporada
    inicio_year, fin_year = parse_temporada(temporada)

    # Ventana no solapada entre temporadas: 1 julio -> 30 junio.
    inicio_global = f"{inicio_year}-07-01"
    fin_global = f"{fin_year}-06-30"

    inicio_ts = int(
        datetime(
            inicio_year, 7, 1, 0, 0,
            tzinfo=timezone.utc
        ).timestamp()
    )
    fin_ts = int(
        datetime(
            fin_year, 6, 30, 23, 59, 59,
            tzinfo=timezone.utc
        ).timestamp()
    )

    api = ApiIngesta()
    sf = Sofascore(api)

    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    ya = api.contexto_historico_estado(temporada)
    print(f"Partidos Sofascore ya conocidos en {temporada}: {len(ya)}")

    # El lote se abre DESPUÉS del descubrimiento. Así un bloqueo externo
    # (403, DNS, etc.) no deja lotes abiertos sin haber guardado ningún dato.
    lote_id: int | None = None

    target_ids: dict[str, set[int]] = {
        "MASCULINO": set(),
        "FEMENINO": set(),
    }
    eventos: dict[int, dict] = {}
    discovery_errors: list[str] = []

    print("\n1) Descubriendo equipos reales de la temporada...")

    for genero, torneos in BASE_TOURNAMENTS.items():
        for torneo in torneos:
            try:
                season_id = descubrir_season_id(
                    sf, int(torneo["id"]), temporada
                )
                evs = eventos_torneo_temporada(
                    sf, int(torneo["id"]), season_id
                )
                evs = [
                    e for e in evs
                    if dentro_ventana(e, inicio_ts, fin_ts)
                ]
                print(
                    f"   {torneo['nombre']}: "
                    f"season_id={season_id}, partidos={len(evs)}"
                )

                for e in evs:
                    eid = int(e["id"])
                    eventos[eid] = e
                    h, a = equipo_ids_evento(e)
                    if h is not None:
                        target_ids[genero].add(h)
                    if a is not None:
                        target_ids[genero].add(a)

            except Exception as exc:
                msg = f"{torneo['nombre']}: {exc}"
                discovery_errors.append(msg)
                print("   ERROR", msg)

    print(
        "   Equipos objetivo:",
        f"masculino={len(target_ids['MASCULINO'])},",
        f"femenino={len(target_ids['FEMENINO'])}"
    )

    if not target_ids["MASCULINO"]:
        raise RuntimeError("No se descubrieron equipos masculinos.")

    if not target_ids["FEMENINO"]:
        raise RuntimeError("No se descubrieron equipos de Liga F.")

    print("\n2) Recorriendo partidos de cada club para capturar TODAS las competiciones...")

    errores_equipos: list[str] = []

    todos_target = [
        ("MASCULINO", x) for x in sorted(target_ids["MASCULINO"])
    ] + [
        ("FEMENINO", x) for x in sorted(target_ids["FEMENINO"])
    ]

    for idx, (genero, equipo_id) in enumerate(todos_target, start=1):
        try:
            evs = eventos_equipo_ventana(
                sf, equipo_id, inicio_ts, fin_ts
            )
            añadidos = 0
            for e in evs:
                if es_amistoso(e):
                    continue
                eid = int(e["id"])
                if eid not in eventos:
                    añadidos += 1
                eventos[eid] = e

            print(
                f"   [{idx}/{len(todos_target)}] "
                f"{genero} team={equipo_id}: "
                f"{len(evs)} en ventana, +{añadidos}"
            )
        except Exception as exc:
            msg = f"team {equipo_id}: {exc}"
            errores_equipos.append(msg)
            print("   ERROR", msg)

    # Solo eventos oficiales en ventana y que involucren a algún target.
    seleccion: list[tuple[dict, str]] = []

    for e in eventos.values():
        if es_amistoso(e) or not dentro_ventana(e, inicio_ts, fin_ts):
            continue

        h, a = equipo_ids_evento(e)
        genero = None
        if h in target_ids["FEMENINO"] or a in target_ids["FEMENINO"]:
            genero = "FEMENINO"
        elif h in target_ids["MASCULINO"] or a in target_ids["MASCULINO"]:
            genero = "MASCULINO"

        if genero:
            seleccion.append((e, genero))

    seleccion.sort(key=lambda x: ts(x[0]) or 0)

    print(
        f"\n3) Guardando {len(seleccion)} partidos oficiales "
        f"de {temporada}..."
    )

    lote_id = api.iniciar_lote(
        fuente=f"sofascore-historico-{temporada}",
        tipo_fuente="api",
        notas=(
            f"Histórico completo por equipos {temporada}: "
            "competiciones oficiales, partidos, alineaciones, minutos y stats."
        ),
    )
    print("Lote abierto:", lote_id)

    partidos_ok = 0
    detalles_ok = 0
    detalles_omitidos = 0
    errores_partido: list[str] = []
    errores_detalle: list[str] = []
    competiciones_vistas: set[str] = set()

    for i, (evento, genero) in enumerate(seleccion, start=1):
        event_id = int(evento["id"])
        estado = map_estado(evento)
        h, a = equipo_ids_evento(evento)
        if h is None or a is None:
            errores_partido.append(f"{event_id}: sin equipos")
            continue

        nombre_comp, *_ = info_competicion(evento, genero)
        competiciones_vistas.add(
            f"{genero}:{nombre_comp}"
        )

        try:
            payload = payload_partido(
                evento,
                temporada,
                genero,
                inicio_global,
                fin_global,
            )
            payload["lote_id"] = lote_id
            res = api.guardar_partido_historico(payload)
            partidos_ok += 1

            if i == 1 or i % 50 == 0 or i == len(seleccion):
                print(
                    f"   partido {i}/{len(seleccion)} "
                    f"ok={partidos_ok}, detalles={detalles_ok}"
                )

        except Exception as exc:
            errores_partido.append(
                f"{event_id} {nombre_comp}: {exc}"
            )
            print(
                f"   ERROR partido {event_id} "
                f"{nombre_comp}: {exc}"
            )
            continue

        # Solo los finalizados pueden aportar carga real.
        if estado != "FINALIZADO":
            detalles_omitidos += 1
            continue

        conocido = ya.get(str(event_id))
        if conocido:
            try:
                if int(conocido.get("jugadores_stats") or 0) >= 14:
                    detalles_omitidos += 1
                    continue
            except (TypeError, ValueError):
                pass

        try:
            lineups, statistics, jugadores, equipos_stats = detalle_evento(
                sf, event_id, h, a
            )

            # Un 404 de lineups/statistics es cobertura ausente, no error.
            if lineups is None and statistics is None:
                detalles_omitidos += 1
                continue

            detalle_payload = {
                "lote_id": lote_id,
                "fuente": "sofascore",
                "id_partido_fuente": str(event_id),
                "temporada_etiqueta": temporada,
                "fecha_partido": fecha_local_madrid(int(ts(evento))),
                "jugadores": jugadores,
                "equipos_stats": equipos_stats,
                "url_lineups": f"{SOFASCORE_CANONICO}/event/{event_id}/lineups"
                if lineups is not None else None,
                "raw_lineups": json.dumps(
                    lineups,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) if lineups is not None else None,
                "url_statistics": f"{SOFASCORE_CANONICO}/event/{event_id}/statistics"
                if statistics is not None else None,
                "raw_statistics": json.dumps(
                    statistics,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) if statistics is not None else None,
            }

            api.guardar_detalle_historico(detalle_payload)
            detalles_ok += 1

        except Exception as exc:
            errores_detalle.append(
                f"{event_id} {nombre_comp}: {exc}"
            )
            print(
                f"   AVISO detalle {event_id}: {exc}"
            )

        time.sleep(0.03)

    estado_lote = "completado"
    if errores_partido or errores_equipos or discovery_errors:
        estado_lote = "parcial"

    notas = (
        f"temporada={temporada}; "
        f"partidos={partidos_ok}/{len(seleccion)}; "
        f"detalles_ok={detalles_ok}; "
        f"detalles_omitidos={detalles_omitidos}; "
        f"errores_partido={len(errores_partido)}; "
        f"errores_equipos={len(errores_equipos)}; "
        f"errores_discovery={len(discovery_errors)}; "
        f"errores_detalle={len(errores_detalle)}"
    )

    api.finalizar_lote(
        lote_id,
        estado=estado_lote,
        notas=notas[:1500],
    )

    print("\nCOMPETICIONES CAPTURADAS")
    for x in sorted(competiciones_vistas):
        print("  -", x)

    print(
        "\nRESUMEN:",
        f"temporada={temporada}",
        f"partidos={partidos_ok}/{len(seleccion)}",
        f"detalles={detalles_ok}",
        f"sin_detalle={detalles_omitidos}",
        f"errores_partido={len(errores_partido)}",
        f"errores_equipo={len(errores_equipos)}",
        f"errores_detalle={len(errores_detalle)}",
    )

    # Detalles pueden faltar legítimamente en competiciones con baja cobertura.
    # Pero no aceptamos huecos de partidos/equipos/discovery con check verde.
    errores_criticos = (
        len(errores_partido)
        + len(errores_equipos)
        + len(discovery_errors)
    )
    if errores_criticos:
        ejemplos = (
            discovery_errors
            + errores_equipos
            + errores_partido
        )[:3]
        raise RuntimeError(
            f"Temporada {temporada} terminó con "
            f"{errores_criticos} error(es) críticos. "
            f"Ejemplos: {' | '.join(ejemplos)}"
        )


if __name__ == "__main__":
    main()
