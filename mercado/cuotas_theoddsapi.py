"""
Captura de cuotas 1X2 desde The Odds API y volcado a mercado_cuotas_capturadas.

Uso:
    python cuotas_theoddsapi.py                 # captura y guarda
    python cuotas_theoddsapi.py --dry-run       # muestra qué haría, no escribe
    python cuotas_theoddsapi.py --listar-ligas  # descubre sport keys (gratis)

Variables de entorno:
    ODDS_API_KEY        clave de the-odds-api.com
    INGEST_API_URL      puente PHP de IONOS
    INGEST_API_TOKEN    token del puente

Coste de cuota en The Odds API:
    /v4/sports/          gratis, no consume
    /v4/sports/X/odds/   [nº mercados] x [nº regiones] = 1 con h2h + eu

Con 500 créditos/mes y 2 ligas, una captura diaria gasta ~60/mes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
from api_client import ApiIngesta  # noqa: E402


ODDS_BASE_URL = "https://api.the-odds-api.com/v4"

# Ligas que nos interesan. La clave es el sport_key de The Odds API;
# el valor es una etiqueta para los mensajes.
LIGAS_OBJETIVO = {
    "soccer_spain_la_liga": "LaLiga",
    "soccer_spain_segunda_division": "Segunda División",
}

# Palabras que no distinguen a un club y estorban al emparejar.
# 'real' NO está: distingue Real Madrid de Real Sociedad de Real Betis.
RUIDO_NOMBRE = {
    "fc", "cf", "cd", "rcd", "ud", "sd", "rc", "ca", "ce", "ad", "club",
    "de", "deportivo", "futbol", "balompie",
}

# Discrepancias reales entre cómo nombra los clubes The Odds API y cómo los
# nombra nucleo_equipos. La clave es el nombre ya normalizado; el valor, la
# forma canónica a la que se reducen ambas variantes.
#
# Se comparan nombres completos, no palabras sueltas: así 'deportivo alaves'
# no colisiona con 'rc deportivo'.
# Nota: 'Atlético de Madrid' y 'Atletico Madrid' ya convergen solos en
# 'atletico madrid' al quitar la preposición, y no se abrevian a 'atletico'
# a propósito: acortarlo lo acercaría peligrosamente a 'athletic'.
ALIAS_CLUBES = {
    "athletic bilbao": "athletic",
    "espanyol barcelona": "espanyol",
    "celta vigo": "celta",
    "la coruna": "deportivo coruna",
    "rc deportivo": "deportivo coruna",
    "r racing": "racing",
    "real racing": "racing",
}

# Emparejamiento temporal.
#
# The Odds API publica una hora provisional (típicamente las 19:00 del sábado)
# para partidos cuyo horario aún no ha confirmado LaLiga. La BD, que se nutre
# de laliga.com, sí tiene la hora real. Por eso el mismo partido puede aparecer
# con 24-48 h de diferencia entre ambas fuentes.
#
# La solución es una ventana en dos tramos: cerca en el tiempo basta un
# parecido razonable de nombres; lejos, se exige que los nombres sean casi
# idénticos. Así no se cuela la vuelta de un enfrentamiento por la ida.
VENTANA_ESTRICTA_HORAS = 6
VENTANA_AMPLIA_HORAS = 60
UMBRAL_SIMILITUD = 0.72
UMBRAL_SIMILITUD_LEJOS = 0.90


# ---------------------------------------------------------------------------
# Normalización y emparejamiento de nombres
# ---------------------------------------------------------------------------

def normalizar(nombre: str) -> str:
    """
    Reduce un nombre de club a su forma comparable.

    Quita acentos, puntuación y palabras que no distinguen al club, pero
    nunca deja el nombre vacío: en 'RC Deportivo' la palabra 'deportivo' es
    el nombre, no ruido. Después aplica los alias conocidos.
    """
    txt = unicodedata.normalize("NFKD", nombre)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = txt.lower()
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)

    palabras = [p for p in txt.split() if p]
    filtradas = [p for p in palabras if p not in RUIDO_NOMBRE]

    # Si el filtro se lo come todo, el 'ruido' era en realidad el nombre.
    base = " ".join(filtradas) if filtradas else " ".join(palabras)

    return ALIAS_CLUBES.get(base, base)


def similitud(a: str, b: str) -> float:
    """Similitud 0..1 entre dos nombres ya normalizados."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Si uno contiene al otro entero, es casi seguro el mismo club
    # ('espanyol' dentro de 'espanyol femenino').
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Estructuras
# ---------------------------------------------------------------------------

@dataclass
class PartidoBD:
    """Un partido de nucleo_partidos que espera cuotas."""
    partido_id: int
    local: str
    visitante: str
    inicio: datetime
    competicion: str

    local_norm: str = field(init=False)
    visitante_norm: str = field(init=False)

    def __post_init__(self) -> None:
        self.local_norm = normalizar(self.local)
        self.visitante_norm = normalizar(self.visitante)


@dataclass
class EventoOdds:
    """Un evento devuelto por The Odds API."""
    evento_id: str
    local: str
    visitante: str
    inicio: datetime
    casas: list[dict]

    local_norm: str = field(init=False)
    visitante_norm: str = field(init=False)

    def __post_init__(self) -> None:
        self.local_norm = normalizar(self.local)
        self.visitante_norm = normalizar(self.visitante)


def franja_temporal(inicio: datetime, ahora: datetime) -> str:
    """Etiqueta la antelación de la captura respecto al inicio del partido."""
    horas = (inicio - ahora).total_seconds() / 3600
    if horas > 72:
        return "T-72+"
    if horas > 48:
        return "T-72"
    if horas > 12:
        return "T-24"
    if horas > 2:
        return "T-2"
    if horas > 0:
        return "CIERRE"
    return "POST"


# ---------------------------------------------------------------------------
# Cliente de The Odds API
# ---------------------------------------------------------------------------

class OddsApi:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError("Falta ODDS_API_KEY.")
        self.api_key = api_key
        self.session = requests.Session()
        self.creditos_usados = 0
        self.creditos_restantes: int | None = None

    def _get(self, ruta: str, params: dict) -> list[dict]:
        params = {**params, "apiKey": self.api_key}
        url = f"{ODDS_BASE_URL}{ruta}"

        r = self.session.get(url, params=params, timeout=30)

        # Cabeceras de cuota: informan aunque la petición falle.
        usados = r.headers.get("x-requests-last")
        if usados is not None:
            try:
                self.creditos_usados += int(usados)
            except ValueError:
                pass
        restantes = r.headers.get("x-requests-remaining")
        if restantes is not None:
            try:
                self.creditos_restantes = int(restantes)
            except ValueError:
                pass

        if r.status_code == 401:
            raise RuntimeError("ODDS_API_KEY inválida o caducada (HTTP 401).")
        if r.status_code == 422:
            raise RuntimeError(f"Parámetros rechazados por la API: {r.text[:300]}")
        if r.status_code == 429:
            raise RuntimeError("Cuota de The Odds API agotada (HTTP 429).")
        if not r.ok:
            raise RuntimeError(f"The Odds API HTTP {r.status_code}: {r.text[:300]}")

        datos = r.json()
        if not isinstance(datos, list):
            raise RuntimeError(f"Respuesta inesperada de The Odds API: {datos}")
        return datos

    def listar_deportes(self) -> list[dict]:
        """No consume cuota."""
        return self._get("/sports/", {"all": "true"})

    def odds_de_liga(self, sport_key: str) -> list[EventoOdds]:
        datos = self._get(
            f"/sports/{sport_key}/odds/",
            {
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )

        eventos = []
        for d in datos:
            try:
                inicio = datetime.fromisoformat(
                    d["commence_time"].replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                continue

            eventos.append(
                EventoOdds(
                    evento_id=d.get("id", ""),
                    local=d.get("home_team", ""),
                    visitante=d.get("away_team", ""),
                    inicio=inicio,
                    casas=d.get("bookmakers", []),
                )
            )
        return eventos


# ---------------------------------------------------------------------------
# Extracción de cuotas 1X2
# ---------------------------------------------------------------------------

def extraer_1x2(casa: dict, local: str, visitante: str) -> dict | None:
    """
    Saca las tres cuotas del mercado h2h de una casa, con su timestamp.

    En h2h de fútbol los outcomes son: nombre del local, nombre del
    visitante y 'Draw'. Si falta alguno, la fila no sirve.

    Devuelve también 'actualizado_en': el momento en que ESA casa movió la
    cuota, no el momento en que nosotros consultamos. Es lo que hace la
    captura idempotente: dos ejecuciones seguidas sobre una cuota que no se
    ha movido escriben la misma fila en lugar de duplicarla.
    """
    for mercado in casa.get("markets", []):
        if mercado.get("key") != "h2h":
            continue

        cuotas: dict[str, float] = {}
        for out in mercado.get("outcomes", []):
            nombre = out.get("name", "")
            precio = out.get("price")
            if precio is None:
                continue

            if nombre.lower() == "draw":
                cuotas["empate"] = float(precio)
            elif nombre == local:
                cuotas["local"] = float(precio)
            elif nombre == visitante:
                cuotas["visitante"] = float(precio)

        if len(cuotas) != 3:
            continue

        marca = casa.get("last_update") or mercado.get("last_update")
        if not marca:
            return None

        try:
            cuotas["actualizado_en"] = datetime.fromisoformat(
                marca.replace("Z", "+00:00")
            )
        except ValueError:
            return None

        return cuotas

    return None


def resolver_emparejamientos(
    eventos: list[EventoOdds],
    partidos: list[PartidoBD],
) -> tuple[
    dict[str, PartidoBD],
    dict[str, float],
    list[EventoOdds],
    list[tuple[EventoOdds, float]],
]:
    """
    Asigna como mucho un evento a cada partido, y viceversa.

    The Odds API publica algunos partidos dos veces: una con el horario ya
    confirmado y otra con uno provisional. Sin exclusión mutua, ambos
    escriben sobre el mismo partido y el segundo pisa las cuotas del primero.

    Se resuelve de forma voraz: se ordenan todas las parejas posibles por
    calidad (primero mejor parecido de nombres, luego menor distancia
    temporal) y se van asignando mientras ni el evento ni el partido estén
    ya cogidos.
    """
    parejas = []
    for ev in eventos:
        for p in partidos:
            delta = abs((p.inicio - ev.inicio).total_seconds()) / 3600
            if delta > VENTANA_AMPLIA_HORAS:
                continue

            score = (
                similitud(ev.local_norm, p.local_norm)
                + similitud(ev.visitante_norm, p.visitante_norm)
            ) / 2

            umbral = (
                UMBRAL_SIMILITUD
                if delta <= VENTANA_ESTRICTA_HORAS
                else UMBRAL_SIMILITUD_LEJOS
            )
            if score < umbral:
                continue

            parejas.append((score, -delta, ev, p))

    # Mejor puntuación primero; a igualdad, la fecha más cercana.
    parejas.sort(key=lambda t: (t[0], t[1]), reverse=True)

    asignado: dict[str, PartidoBD] = {}
    puntuacion: dict[str, float] = {}
    partidos_cogidos: set[int] = set()

    # Eventos que sí tenían al menos un candidato válido, aunque acaben
    # sin asignar porque otro evento se quedó antes con ese partido.
    tenian_candidato: set[str] = set()

    for score, neg_delta, ev, p in parejas:
        tenian_candidato.add(ev.evento_id)
        if ev.evento_id in asignado:
            continue
        if p.partido_id in partidos_cogidos:
            continue
        asignado[ev.evento_id] = p
        puntuacion[ev.evento_id] = score
        partidos_cogidos.add(p.partido_id)

    duplicados: list[EventoOdds] = []
    sin_candidato: list[tuple[EventoOdds, float]] = []

    for ev in eventos:
        if ev.evento_id in asignado:
            continue

        if ev.evento_id in tenian_candidato:
            # Tenía pareja válida, pero su partido ya estaba cogido: es el
            # mismo encuentro publicado dos veces por la API.
            duplicados.append(ev)
            continue

        mejor = max(
            (
                (
                    similitud(ev.local_norm, p.local_norm)
                    + similitud(ev.visitante_norm, p.visitante_norm)
                ) / 2
                for p in partidos
            ),
            default=0.0,
        )
        sin_candidato.append((ev, mejor))

    return asignado, puntuacion, duplicados, sin_candidato


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Captura cuotas 1X2 y las guarda en MariaDB vía IONOS."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="muestra lo que haría sin escribir nada",
    )
    parser.add_argument(
        "--listar-ligas",
        action="store_true",
        help="lista los sport keys disponibles y sale (no consume cuota)",
    )
    parser.add_argument(
        "--dias-futuro",
        type=int,
        default=14,
        help="ventana de partidos a considerar (por defecto 14)",
    )
    args = parser.parse_args()

    odds = OddsApi(os.environ.get("ODDS_API_KEY", "").strip())

    # --- Modo descubrimiento -------------------------------------------

    if args.listar_ligas:
        deportes = odds.listar_deportes()
        futbol = [d for d in deportes if d.get("key", "").startswith("soccer_")]
        espanoles = [
            d for d in futbol
            if "spain" in d.get("key", "") or "Spain" in d.get("title", "")
        ]

        print(f"Ligas de fútbol disponibles: {len(futbol)}")
        print(f"\nCompeticiones españolas ({len(espanoles)}):")
        for d in sorted(espanoles, key=lambda x: x.get("key", "")):
            activo = "activa " if d.get("active") else "inactiva"
            print(f"  [{activo}] {d['key']:<45} {d.get('title', '')}")

        print("\nBuscando femenino:")
        fem = [
            d for d in futbol
            if "women" in d.get("key", "").lower()
            or "women" in d.get("title", "").lower()
        ]
        if fem:
            for d in sorted(fem, key=lambda x: x.get("key", "")):
                activo = "activa " if d.get("active") else "inactiva"
                print(f"  [{activo}] {d['key']:<45} {d.get('title', '')}")
        else:
            print("  ninguna liga femenina en el catálogo")

        return 0

    # --- Partidos que esperan cuotas ------------------------------------

    api = ApiIngesta()
    pendientes_raw = api.contexto_cuotas(
        dias_futuro=args.dias_futuro,
        solo_sin_cuotas=False,
        limite=500,
    )

    partidos = []
    for p in pendientes_raw:
        try:
            inicio = datetime.fromisoformat(p["fecha_hora_inicio"])
            # La BD guarda hora de Madrid; la API devuelve UTC.
            inicio = inicio.replace(tzinfo=timezone(timedelta(hours=2)))
        except (KeyError, ValueError):
            continue

        partidos.append(
            PartidoBD(
                partido_id=int(p["partido_id"]),
                local=p["equipo_local"],
                visitante=p["equipo_visitante"],
                inicio=inicio,
                competicion=p.get("competicion", ""),
            )
        )

    print(f"Partidos en BD dentro de {args.dias_futuro} días: {len(partidos)}")

    # --- Captura por liga ------------------------------------------------

    # Instante único de la ejecución. La franja temporal es una propiedad
    # del partido, no de cada casa: si se calculara desde el last_update
    # de cada bookmaker, un partido situado justo en un umbral (48 h, por
    # ejemplo) quedaría clasificado en franjas distintas según qué casa lo
    # mirase, y cada ejecución generaría filas nuevas sin que la cuota se
    # haya movido.
    ahora = datetime.now(timezone.utc)

    guardados = 0
    fallos = 0
    sin_emparejar: list[tuple[str, str, float]] = []
    descartados_dup: list[tuple[str, str]] = []

    for sport_key, etiqueta in LIGAS_OBJETIVO.items():
        print(f"\n{etiqueta} ({sport_key})")

        try:
            eventos = odds.odds_de_liga(sport_key)
        except RuntimeError as exc:
            print(f"  ERROR: {exc}")
            continue

        print(f"  eventos devueltos: {len(eventos)}")

        asignado, puntuacion, duplicados, sin_candidato = (
            resolver_emparejamientos(eventos, partidos)
        )

        if duplicados:
            print(
                f"  {len(duplicados)} evento(s) que la API publica por"
                f" duplicado; se conserva el mejor de cada partido"
            )

        for ev in eventos:
            partido = asignado.get(ev.evento_id)
            if partido is None:
                continue

            score = puntuacion[ev.evento_id]
            franja = franja_temporal(partido.inicio, ahora)

            casas_ok = 0

            for casa in ev.casas:
                cuotas = extraer_1x2(casa, ev.local, ev.visitante)
                if cuotas is None:
                    continue

                # capturado_en registra cuándo el proveedor tenía este
                # precio. Es informativo: quien decide si hay fila nueva es la
                # comparación de cuotas que hace guardar_cuota.php.
                momento = cuotas["actualizado_en"]

                payload = {
                    "partido_id": partido.partido_id,
                    "casa_apuestas": casa.get("key", "desconocida")[:80],
                    "mercado": "1X2",
                    "capturado_en": momento.astimezone(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "franja_temporal": franja,
                    "cuota_local": cuotas["local"],
                    "cuota_empate": cuotas["empate"],
                    "cuota_visitante": cuotas["visitante"],
                    "fuente": "the-odds-api",
                }

                if args.dry_run:
                    casas_ok += 1
                    continue

                try:
                    api.guardar_cuota(payload)
                    casas_ok += 1
                    guardados += 1
                except RuntimeError as exc:
                    fallos += 1
                    print(f"    fallo al guardar ({casa.get('key')}): {exc}")

            marca = "~" if args.dry_run else "+"
            print(
                f"  {marca} [{score:.2f}] {partido.local} vs {partido.visitante}"
                f"  ({franja}, {casas_ok} casas)"
            )

        sin_emparejar.extend(
            (ev.local, ev.visitante, score) for ev, score in sin_candidato
        )
        descartados_dup.extend(
            (ev.local, ev.visitante) for ev in duplicados
        )

    # --- Resumen ---------------------------------------------------------

    print("\n" + "-" * 60)
    if args.dry_run:
        print("DRY RUN: no se ha escrito nada en la base de datos")
    else:
        print(f"Filas guardadas: {guardados}")
        if fallos:
            print(f"Fallos al guardar: {fallos}")

    if descartados_dup:
        print(
            f"\nDuplicados de la API descartados ({len(descartados_dup)}):"
        )
        for local, visitante in descartados_dup:
            print(f"  {local} vs {visitante}")
        print(
            "\n  La API publica algunos partidos dos veces, con horario\n"
            "  confirmado y provisional. Se ha guardado una sola vez. Esto es\n"
            "  normal y no requiere acción."
        )

    if sin_emparejar:
        print(f"\nEventos sin partido en la BD ({len(sin_emparejar)}):")
        for local, visitante, score in sin_emparejar:
            print(f"  [mejor parecido: {score:.2f}] {local} vs {visitante}")
        print(
            "\n  El partido no está cargado, cae fuera de la ventana, o los\n"
            "  nombres difieren demasiado. Si el parecido es alto, añadir el\n"
            "  club a ALIAS_CLUBES antes que bajar UMBRAL_SIMILITUD."
        )

    print(f"\nCréditos consumidos en esta ejecución: {odds.creditos_usados}")
    if odds.creditos_restantes is not None:
        print(f"Créditos restantes en el plan: {odds.creditos_restantes}")

    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
