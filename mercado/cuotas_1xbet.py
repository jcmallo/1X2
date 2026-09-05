"""
Cuotas de Liga F desde la API de línea de 1xBet.

Por qué esta fuente
-------------------

Ninguna de las APIs que pagamos cubre la liga femenina española, y el
comparador que usábamos (sportytrader) solo publica dos de los ocho partidos
de cada jornada. 1xBet los tiene todos, y además los sirve como JSON
estructurado en lugar de HTML, así que no depende de que no cambien el
diseño de una página.

Comprobado sobre la jornada 4, donde faltaban tres casillas:

    DUX Logroño (F) - Athletic (F)      2.937 / 3.50 / 2.16
    Real Madrid (F) - Eibar (F)          1.06 / 8.30 / 29.0
    At. Madrid (F) - Alavés (F)          1.30 / 5.50 / 7.15

Cómo está montada la respuesta
------------------------------

    Get1x2_VZip?champs=124885   la lista de partidos del campeonato
      I    identificador del partido
      L    nombre de la liga
      O1   equipo local          O2   equipo visitante
      S    inicio, en segundos desde epoch
      E    mercados, cada uno con G (grupo), T (tipo) y C (cuota)

El 1X2 es el grupo 1: T=1 local, T=2 empate, T=3 visitante.

Una casa, no un consenso
------------------------

Esto es una sola casa, y se guarda como tal (`casa_apuestas = '1xbet'`) para
que después se pueda medir si acierta más o menos que los exchanges. Su
margen ronda el 6-8% frente al 0,8% de Betfair; al pasar de cuota a
probabilidad se normaliza y desaparece, pero un margen mayor significa una
estimación algo más ruidosa. Donde haya exchange, el exchange manda.

Uso
---

    python -m mercado.cuotas_1xbet --dry-run
    python -m mercado.cuotas_1xbet --franja T-24
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_client import ApiIngesta  # noqa: E402

try:
    from curl_cffi import requests as cr
except ImportError:  # pragma: no cover
    cr = None
    import requests as cr


BASE = "https://1xbet.es/service/LineFeed"

# Identificador del campeonato en 1xBet. Se saca de la URL que pide la web
# al abrir la página de la competición.
CAMPEONATOS = {
    "Liga F": 124885,
}

CASA = "1xbet"

HUELLAS = ["chrome124", "chrome120", "safari17_0"]

# El 1X2 vive en el grupo 1; dentro, el tipo dice a qué signo corresponde.
GRUPO_1X2 = 1
TIPOS = {1: "cuota_local", 2: "cuota_empate", 3: "cuota_visitante"}

# Margen máximo aceptable. 1xBet suele moverse entre el 5% y el 10%; por
# encima del 20% es que se ha leído mal alguna cuota.
MARGEN_MAXIMO = 0.20

PAUSA = 0.5

RUIDO = {"fc", "cf", "cd", "rcd", "ud", "sd", "rc", "ca", "ce", "ad", "club",
         "de", "la", "femenino", "femeni", "women", "w", "cff"}

# Cómo nombra 1xBet a los equipos frente a nucleo_equipos.
ALIAS = {
    "barcelona": "barcelona",
    "sevilla": "sevilla",
    "costa adeje tenerife": "tenerife",
    "tenerife": "tenerife",
    "deportivo a coruna": "deportivo",
    "deportivo de a coruna": "deportivo",
    "deportivo abanca": "deportivo",
    "athletic bilbao": "athletic",
    "athletic": "athletic",
    "dux logrono": "logrono united",
    "logrono": "logrono united",
    "real madrid": "real madrid",
    "atletico madrid": "atletico madrid",
    "atletico de madrid": "atletico madrid",
    "real sociedad": "real sociedad",
    "madrid cff": "madrid cff",
    "levante las planas": "levante planas",
    "badalona": "badalona",
    "eibar": "eibar",
    "granada": "granada",
    "espanyol": "espanyol",
    "valencia": "valencia",
    "alaves": "alaves",
    "deportivo alaves": "alaves",
}

UMBRAL = 0.70


def pedir(url: str, params: dict) -> dict:
    """GET con huella de navegador. 1xBet filtra las peticiones sin ella."""
    ultimo = None
    for huella in HUELLAS:
        try:
            try:
                r = cr.get(url, params=params, impersonate=huella, timeout=30)
            except TypeError:
                # requests a secas no acepta impersonate; se usa como respaldo
                r = cr.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            ultimo = f"HTTP {r.status_code} con {huella}"
        except Exception as exc:  # noqa: BLE001
            ultimo = f"{huella}: {exc}"
    raise RuntimeError(f"No se pudo leer {url}. Último intento: {ultimo}")


def normalizar(nombre: str) -> str:
    # 1xBet marca la competición con "(F)" o "(Women)".
    nombre = re.sub(r"\((?:F|W|Women|Femenino)\)", " ", nombre, flags=re.I)
    txt = unicodedata.normalize("NFKD", nombre)
    txt = "".join(c for c in txt if not unicodedata.combining(c)).lower()
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)
    palabras = [p for p in txt.split() if p]
    if " ".join(palabras) in ALIAS:
        return ALIAS[" ".join(palabras)]
    filtradas = [p for p in palabras if p not in RUIDO]
    base = " ".join(filtradas) if filtradas else " ".join(palabras)
    return ALIAS.get(base, base)


def parecido(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.94
    return SequenceMatcher(None, a, b).ratio()


def leer_1x2(partido: dict) -> dict | None:
    """
    Las tres cuotas del grupo 1, si están las tres y son coherentes.

    Se exigen las tres: con dos no se puede normalizar el margen, y guardar
    una probabilidad a la que le falta un signo sería peor que no guardarla.
    """
    cuotas = {}
    for e in partido.get("E", []):
        if e.get("G") != GRUPO_1X2:
            continue
        clave = TIPOS.get(e.get("T"))
        if clave and isinstance(e.get("C"), (int, float)):
            cuotas[clave] = float(e["C"])

    if len(cuotas) != 3:
        return None
    if any(c < 1.01 or c > 1000 for c in cuotas.values()):
        return None

    margen = sum(1 / c for c in cuotas.values()) - 1
    if not (0.0 <= margen <= MARGEN_MAXIMO):
        return None

    cuotas["margen"] = round(margen, 4)
    return cuotas


def partidos_de(campeonato: int) -> list[dict]:
    d = pedir(f"{BASE}/Get1x2_VZip", {
        "champs": campeonato,
        "count": 50,
        "lng": "es",
        "mode": 4,
        "country": 78,
        "partner": 229,
        "getEmpty": "true",
        # No cambia la respuesta de Liga F, pero es el parámetro que manda
        # la propia web y así la petición es idéntica a la suya.
        "virtualSports": "true",
    })
    return d.get("Value") or []


def main() -> int:
    p = argparse.ArgumentParser(description="Captura cuotas de Liga F desde 1xBet.")
    p.add_argument("--franja", default="T-24", help="franja temporal de la captura")
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    args = p.parse_args()

    api = ApiIngesta()

    pendientes = api.contexto_cuotas(
        dias_futuro=10, retro_horas=6, solo_sin_cuotas=False, limite=80,
        genero="FEMENINO",
    )
    print(f"Partidos de Liga F en la base: {len(pendientes)}")
    if not pendientes:
        print("No hay partidos próximos.")
        return 0

    guardadas = 0
    sin_emparejar = []

    for etiqueta, champ in CAMPEONATOS.items():
        print(f"\nLeyendo {etiqueta} (campeonato {champ})...")
        try:
            juegos = partidos_de(champ)
        except RuntimeError as exc:
            print(f"  {exc}")
            return 1

        print(f"  {len(juegos)} partidos publicados\n")

        for g in juegos:
            local = str(g.get("O1", ""))
            visitante = str(g.get("O2", ""))
            if not local or not visitante:
                continue

            cuotas = leer_1x2(g)
            if not cuotas:
                print(f"  {local[:22]:<22} - {visitante[:22]:<22} sin 1X2 completo")
                continue

            nl, nv = normalizar(local), normalizar(visitante)
            mejor, score = None, 0.0
            for p_ in pendientes:
                s = (parecido(nl, normalizar(p_["equipo_local"]))
                     + parecido(nv, normalizar(p_["equipo_visitante"]))) / 2
                if s > score:
                    score, mejor = s, p_

            if not mejor or score < UMBRAL:
                sin_emparejar.append((local, visitante, mejor, score))
                continue

            inv = [
                1 / cuotas["cuota_local"],
                1 / cuotas["cuota_empate"],
                1 / cuotas["cuota_visitante"],
            ]
            t = sum(inv)
            print(
                f"  [{score:.2f}] {local[:20]:<20} - {visitante[:20]:<20} "
                f"{cuotas['cuota_local']}/{cuotas['cuota_empate']}/{cuotas['cuota_visitante']}"
                f"  →  {inv[0]/t*100:.0f}/{inv[1]/t*100:.0f}/{inv[2]/t*100:.0f}"
                f"  (margen {cuotas['margen']:.1%})"
            )

            if args.dry_run:
                continue

            api.guardar_cuota({
                "partido_id": int(mejor["partido_id"]),
                "casa_apuestas": CASA,
                "mercado": "1X2",
                "capturado_en": datetime.now(timezone.utc)
                                        .astimezone(timezone(timedelta(hours=2)))
                                        .strftime("%Y-%m-%d %H:%M:%S"),
                "franja_temporal": args.franja,
                "cuota_local": cuotas["cuota_local"],
                "cuota_empate": cuotas["cuota_empate"],
                "cuota_visitante": cuotas["cuota_visitante"],
                "fuente": "1xbet_linefeed",
            })
            guardadas += 1
            time.sleep(PAUSA)

    if sin_emparejar:
        print(f"\n  Sin emparejar ({len(sin_emparejar)}):")
        for l, v, mejor, s in sin_emparejar:
            aprox = (f" — lo más parecido: {mejor['equipo_local']} - "
                     f"{mejor['equipo_visitante']} ({s:.2f})") if mejor else ""
            print(f"    {l} - {v}{aprox}")

    print(f"\n{guardadas} cuotas guardadas.")
    if args.dry_run:
        print("DRY RUN: no se ha escrito nada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
