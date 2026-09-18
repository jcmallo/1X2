"""
Cuotas de Liga F desde eduardolosilla.es, solo para lo que nadie más trae.

Por qué existe
--------------

1xBet cubre los ocho partidos de Liga F casi siempre, pero no cuando el
partido es poco mediático y la casa aún no ha abierto mercado. SportyTrader
publica de por sí solo dos de los ocho. Cuando ninguna de las dos tiene un
partido concreto en el momento de la captura, esa casilla se queda sin
"% Apuestas" en el panel, aunque el resto de la jornada esté completa.

Comprobado en la jornada 8 de 2026-27: EIBAR (F) - ATH.CLUB (F) y
TENERIFE (F) - SEVILLA (F) sin ningún dato nuestro, mientras que
eduardolosilla.es —la misma fuente que usa mundodeportivo.com/servicios/
quiniela para su columna "%probables"— publicaba una probabilidad de mercado
("técnicos") para los cuatro partidos femeninos de esa jornada, incluidos
esos dos.

De dónde sale
-------------

De la API pública (sin autenticación) que alimenta el sitio de Eduardo
Losilla:

    api.eduardolosilla.es/escrutinios?num_jornada=N&num_temporada=AAAA
    api.eduardolosilla.es/probabilidades?num_jornada=N&num_temporada=AAAA

La primera trae los nombres de los quince partidos y en qué posición va
cada uno; la segunda trae, para cada posición, varias series de
porcentajes en paralelo (lo que juega la gente, LAE oficial, y la serie
"técnicos", que es la que se acerca a nuestro "% Apuestas": no hay página
de metodología pública que diga qué casas agrega, así que se trata como una
fuente más, no como la mejor.

Solo se usa donde hace falta
-----------------------------

Este script NUNCA decide por sí mismo si un partido "necesita" el dato de
Losilla: se lo pregunta a la base de datos. `contexto_cuotas.php` con
`solo_sin_cuotas=1` devuelve exactamente los partidos que no tienen ni una
sola fila en `mercado_cuotas_capturadas` todavía. Si 1xBet o SportyTrader ya
capturaron ese partido, ni siquiera aparece en la lista y este script no lo
toca. Por eso en el workflow va DESPUÉS de las otras dos fuentes: para que
"sin cuotas" signifique de verdad "nadie lo ha traído todavía", no "no lo
hemos intentado".

Mismo tratamiento que las demás casas
--------------------------------------

Se guarda con `guardar_cuota.php`, el mismo endpoint que usan 1xBet y
SportyTrader, como una fila más de `mercado_cuotas_capturadas`
(`casa_apuestas='eduardolosilla_tecnicos'`, `fuente='eduardolosilla'`). No
hace falta tocar el panel ni el modelo: `contexto_dashboard.php` ya no
filtra por lista blanca de casas ("cualquier fuente nueva entra sola") y
`pronosticar.py` ya lee ese mismo campo `mercado` para la predicción en
vivo. Esto solo rellena huecos que hoy quedan a null.

Aviso
-----

Es una API no documentada de un competidor directo (Eduardo Losilla vende
pronósticos de quiniela), no un comparador neutral como sportytrader. Puede
cambiar de formato o bloquear peticiones sin aviso, igual que las otras dos
fuentes de Liga F; por eso corre con `continue-on-error` en el workflow y
solo rellena lo que falta, nunca sustituye lo que ya hay.

Uso
---

    python -m mercado.cuotas_losilla --dry-run
    python -m mercado.cuotas_losilla --franja T-24
"""

from __future__ import annotations

import argparse
import os
import re
import sys
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


BASE = "https://api.eduardolosilla.es"

HUELLAS = ["chrome124", "chrome120", "safari17_0"]

UA_NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CASA = "eduardolosilla_tecnicos"
FUENTE = "eduardolosilla"

# Cómo nombra Losilla a los equipos de Liga F frente a nucleo_equipos.
ALIAS = {
    "at madrid": "atletico madrid",
    "atletico madrid": "atletico madrid",
    "r madrid": "real madrid",
    "real madrid": "real madrid",
    "ath club": "athletic",
    "athletic club": "athletic",
    "athletic": "athletic",
    "logrono": "logrono united",
    "r sociedad": "real sociedad",
    "real sociedad": "real sociedad",
    "deportivo": "deportivo",
    "madrid cff": "madrid cff",
    "levante planas": "levante planas",
    "levante las planas": "levante planas",
    "badalona": "badalona",
    "eibar": "eibar",
    "granada": "granada",
    "espanyol": "espanyol",
    "valencia": "valencia",
    "alaves": "alaves",
    "tenerife": "tenerife",
    "sevilla": "sevilla",
}

RUIDO = {"fc", "cf", "cd", "rcd", "ud", "sd", "rc", "ca", "ce", "ad", "club",
         "de", "la", "femenino", "femeni", "women", "w", "cff"}

UMBRAL = 0.70

PAUSA = 0.5


def franja_temporal(inicio: datetime, ahora: datetime) -> str:
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


def normalizar(nombre: str) -> str:
    # Losilla marca el género con "(F)" al final del nombre.
    nombre = re.sub(r"\(\s*F\s*\)", " ", nombre, flags=re.I)
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


def es_liga_f(local: str, visitante: str, division) -> bool:
    if re.search(r"\(\s*F\s*\)", local, re.I) or re.search(r"\(\s*F\s*\)", visitante, re.I):
        return True
    try:
        return int(division) == 10
    except (TypeError, ValueError):
        return False


def pedir_json(url: str, params: dict) -> dict:
    ultimo = None
    for huella in HUELLAS:
        try:
            try:
                r = cr.get(
                    url, params=params, impersonate=huella,
                    headers={"Accept": "application/json", "User-Agent": UA_NAVEGADOR},
                    timeout=30,
                )
            except TypeError:
                # requests a secas no acepta impersonate; se usa como respaldo.
                r = cr.get(
                    url, params=params,
                    headers={"Accept": "application/json", "User-Agent": UA_NAVEGADOR},
                    timeout=30,
                )
            if r.status_code == 200:
                return r.json()
            ultimo = f"HTTP {r.status_code} con {huella}"
        except Exception as exc:  # noqa: BLE001
            ultimo = f"{huella}: {exc}"
    raise RuntimeError(f"No se pudo leer {url}. Último intento: {ultimo}")


def num_temporada_losilla(etiqueta_temporada: str) -> int:
    """'2026-27' -> 2027. Losilla numera la temporada por el año en que acaba."""
    inicio = int(etiqueta_temporada.split("-")[0])
    return inicio + 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Rellena con eduardolosilla.es solo los partidos de Liga F "
        "que ninguna otra fuente ha capturado todavía."
    )
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    p.add_argument("--franja", default="T-24", help="franja temporal de la captura")
    args = p.parse_args()

    api = ApiIngesta()

    # --- Huecos reales, según la propia base de datos -----------------------
    #
    # solo_sin_cuotas=True es la clave de todo el script: si 1xBet o
    # SportyTrader ya trajeron este partido, ni aparece aquí.
    pendientes = api.contexto_cuotas(
        dias_futuro=10, genero="FEMENINO", solo_sin_cuotas=True, limite=80,
    )
    print(f"Partidos de Liga F sin ninguna captura todavía: {len(pendientes)}")
    if not pendientes:
        print("No hay huecos que rellenar. Nada que hacer.")
        return 0
    for c in pendientes:
        print(f"  {c['equipo_local']} - {c['equipo_visitante']}  "
              f"({c['fecha_hora_inicio']})")

    # --- Jornada de quiniela en curso, para pedirle a Losilla la misma -------
    try:
        d = api.contexto_dashboard()
        jornada_actual = d.get("jornada")
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo leer la jornada en curso: {exc}")
        return 1

    if not jornada_actual:
        print("No hay jornada de quiniela cargada; no hay con qué comparar.")
        return 1

    num_jornada = int(jornada_actual["numero"])
    etiqueta_temporada = str(jornada_actual["temporada"])
    num_temporada = num_temporada_losilla(etiqueta_temporada)
    print(f"\nJornada de quiniela: {num_jornada} de {etiqueta_temporada} "
          f"-> Losilla num_jornada={num_jornada} num_temporada={num_temporada}")

    # --- Los partidos y los porcentajes de Losilla ---------------------------
    print("\nLeyendo eduardolosilla.es...")
    try:
        esc = pedir_json(f"{BASE}/escrutinios",
                          {"num_jornada": num_jornada, "num_temporada": num_temporada})
        prob = pedir_json(f"{BASE}/probabilidades",
                           {"num_jornada": num_jornada, "num_temporada": num_temporada})
    except RuntimeError as exc:
        print(f"  {exc}")
        return 1

    partidos_losilla = esc.get("partidos") or []
    tecnicos_por_numero = {
        int(t["numero"]): t for t in (prob.get("partidos", {}).get("tecnicos") or [])
    }

    ligaf_losilla = [
        c for c in partidos_losilla
        if es_liga_f(c.get("local", ""), c.get("visitante", ""), c.get("division"))
    ]
    print(f"  Partidos de Liga F en Losilla: {len(ligaf_losilla)}")
    if not ligaf_losilla:
        print("  Losilla no trae partidos de Liga F para esta jornada. Nada que hacer.")
        return 0

    # --- Emparejar y guardar ---------------------------------------------
    ahora = datetime.now(timezone.utc)
    guardadas = 0
    sin_emparejar = []
    sin_tecnicos = []

    for c in ligaf_losilla:
        nl, nv = normalizar(c.get("local", "")), normalizar(c.get("visitante", ""))

        mejor, score = None, 0.0
        for p_ in pendientes:
            s_ = (parecido(nl, normalizar(p_["equipo_local"]))
                  + parecido(nv, normalizar(p_["equipo_visitante"]))) / 2
            if s_ > score:
                score, mejor = s_, p_

        if not mejor or score < UMBRAL:
            # No es un fallo: lo normal es que la mayoría de los partidos de
            # Losilla YA tengan cuota nuestra y por eso no estén en
            # `pendientes`. Solo interesa saber si alguno de los huecos reales
            # se quedó sin pareja.
            continue

        numero = c.get("num")
        t = tecnicos_por_numero.get(int(numero)) if numero is not None else None
        if not t:
            sin_tecnicos.append((c.get("local"), c.get("visitante")))
            continue

        try:
            p1 = float(t["porc_1_dec"]) / 100
            px = float(t["porc_X_dec"]) / 100
            p2 = float(t["porc_2_dec"]) / 100
        except (KeyError, TypeError, ValueError):
            sin_tecnicos.append((c.get("local"), c.get("visitante")))
            continue

        suma = p1 + px + p2
        if suma <= 0 or abs(suma - 1.0) > 0.03:
            print(f"  {c.get('local')} - {c.get('visitante')}: "
                  f"probabilidades inconsistentes (suman {suma:.3f}), descartado")
            continue
        # Normalizar por si el redondeo de Losilla deja la suma en 99 o 101.
        p1, px, p2 = p1 / suma, px / suma, p2 / suma

        try:
            inicio = datetime.fromisoformat(str(mejor["fecha_hora_inicio"]))
            inicio = inicio.replace(tzinfo=timezone(timedelta(hours=2)))
        except (KeyError, ValueError, TypeError):
            inicio = None
        franja = franja_temporal(inicio, ahora) if inicio else args.franja

        print(
            f"  [{score:.2f}] {c.get('local')[:20]:<20} - {c.get('visitante')[:20]:<20} "
            f"técnicos {p1*100:.0f}/{px*100:.0f}/{p2*100:.0f}  ({franja})"
        )

        if args.dry_run:
            continue

        api.guardar_cuota({
            "partido_id": int(mejor["partido_id"]),
            "casa_apuestas": CASA,
            "mercado": "1X2",
            "capturado_en": ahora.astimezone(timezone(timedelta(hours=2)))
                                  .strftime("%Y-%m-%d %H:%M:%S"),
            "franja_temporal": franja,
            "prob_mercado_local": round(p1, 6),
            "prob_mercado_empate": round(px, 6),
            "prob_mercado_visitante": round(p2, 6),
            "fuente": FUENTE,
        })
        guardadas += 1
        import time
        time.sleep(PAUSA)

    if sin_tecnicos:
        print(f"\n  Sin serie 'técnicos' en Losilla ({len(sin_tecnicos)}):")
        for local, visitante in sin_tecnicos:
            print(f"    {local} - {visitante}")

    print(f"\n{guardadas} huecos rellenados con eduardolosilla.es.")
    if args.dry_run:
        print("DRY RUN: no se ha escrito nada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
