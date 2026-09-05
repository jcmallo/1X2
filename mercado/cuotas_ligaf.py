"""
Cuotas de Liga F, que ninguna API de las que usamos cubre.

Por qué existe
--------------

The Odds API, de donde salen las cuotas de LaLiga y Segunda, no ofrece la
liga femenina española: tiene la Bundesliga femenina y la Champions, pero no
esta. Resultado: cuatro casillas del boleto sin columna de mercado, que son
justo las que el optimizador tiene que decidir a ciegas.

Y no es un hueco menor. Para el Sevilla - Barcelona de la jornada 4:

    público (LAE)      9 / 7 / 84
    modelo propio      8 / 18 / 74
    mercado real       1 / 4 / 95

El mercado da un 95% al Barcelona donde nuestro modelo da un 74%. Veinte
puntos de diferencia en una casilla cambian por completo su valor.

De dónde salen
--------------

De sportytrader.com, un comparador que agrega una docena de casas (Bet365,
Bwin, William Hill, Unibet, 1xBet...). Se toma la MEDIANA de todas ellas y
no la mejor cuota: la mejor suele ser la de la casa más agresiva o la que
tiene un error, y lo que buscamos es el consenso, no la oferta.

Además del 1X2 se lee el mercado de marcador exacto, del que se derivan las
probabilidades del Pleno al 15: sumando por filas y columnas de la matriz de
marcadores salen los goles de cada equipo (0, 1, 2 o M para tres o más).

El control de calidad que no puede faltar
------------------------------------------

El comparador a veces se deja marcadores sin listar. Comprobado: en un
partido faltaba el 0-0, uno de los resultados más probables, y sin él la
distribución del Pleno salía sesgada. Eso es detectable, porque el margen
del mercado se dispara del 16% habitual al 46%. Cuando pasa, se descarta la
captura del Pleno en vez de guardar un dato mal calculado: es la categoría
de premio más alta del boleto.

Uso
---

    python -m mercado.cuotas_ligaf --dry-run
    python -m mercado.cuotas_ligaf
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_client import ApiIngesta  # noqa: E402

try:
    from curl_cffi import requests as cr
except ImportError:  # pragma: no cover
    cr = None


LISTADO = "https://www.sportytrader.com/en/odds/football/spain/liga-femenina-52824/"
BASE = "https://www.sportytrader.com"

# El comparador responde 403 a las peticiones normales: filtra por huella
# TLS, igual que SELAE. curl_cffi reproduce la de un navegador.
HUELLAS = ["chrome124", "chrome120", "safari17_0"]

CASA = "sportytrader_mediana"

# Margen máximo tolerable en el mercado de marcador exacto. Lo normal ronda
# el 16%; por encima de esto faltan marcadores en la página y la
# distribución de goles saldría sesgada.
MARGEN_MAXIMO_PLENO = 0.30

# Y un mínimo: un margen por debajo de cero es imposible y delata que el
# parser ha leído como cuotas algo que no lo era.
MARGEN_MINIMO = 0.0

PAUSA = 1.0

# Cómo nombra el comparador a los equipos frente a nucleo_equipos.
ALIAS = {
    "fc barcelona": "barcelona",
    "sevilla fc": "sevilla",
    "cd tenerife": "tenerife",
    "ud granadilla tenerife": "tenerife",
    "costa adeje tenerife": "tenerife",
    "rc deportivo de la coruna": "deportivo",
    "rc deportivo": "deportivo",
    "deportivo abanca": "deportivo",
    "athletic club": "athletic",
    "real madrid": "real madrid",
    "atletico madrid": "atletico madrid",
    "at madrid": "atletico madrid",
    "real sociedad": "real sociedad",
    "madrid cff": "madrid cff",
    "levante las planas": "levante planas",
    "levante ud": "levante",
    "fc badalona women": "badalona",
    "badalona women": "badalona",
    "logrono": "logrono united",
    "ea eibar": "eibar",
    "sd eibar": "eibar",
}

RUIDO = {"fc", "cf", "cd", "rcd", "ud", "sd", "rc", "ca", "ce", "ad", "club",
         "de", "la", "femenino", "femeni", "women", "w", "cff"}

UMBRAL = 0.70


class Texto(HTMLParser):
    """Saca el texto visible, como hace innerText en el navegador."""

    def __init__(self) -> None:
        super().__init__()
        self.trozos: list[str] = []
        self._saltar = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._saltar = True
        elif tag in ("div", "span", "a", "tr", "td", "p", "section", "img"):
            self.trozos.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._saltar = False

    def handle_data(self, data):
        if not self._saltar and data.strip():
            self.trozos.append(data.strip())

    def texto(self) -> str:
        return "\n".join(t for t in self.trozos if t.strip())


def a_texto(html: str) -> str:
    p = Texto()
    p.feed(html)
    return p.texto()


def pedir(url: str) -> str:
    if cr is None:
        raise RuntimeError(
            "Falta curl_cffi. sportytrader.com rechaza las peticiones sin "
            "huella TLS de navegador. Instálalo con: pip install curl_cffi"
        )
    ultimo = None
    for huella in HUELLAS:
        try:
            r = cr.get(url, impersonate=huella, timeout=40)
            if r.status_code == 200:
                return r.text
            ultimo = f"HTTP {r.status_code} con {huella}"
        except Exception as exc:  # noqa: BLE001
            ultimo = f"{huella}: {exc}"
    raise RuntimeError(f"No se pudo leer {url}. Último intento: {ultimo}")


def normalizar(nombre: str) -> str:
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


def partidos_del_listado(html: str) -> list[dict]:
    """Los partidos anunciados, con su enlace y los dos equipos."""
    salida = []
    vistos = set()
    for m in re.finditer(
        r'href="(/en/odds/[a-z0-9-]+-\d+/)"[^>]*>\s*([^<]+?)\s+-\s+([^<]+?)\s*</a>',
        html,
    ):
        url, local, visitante = m.group(1), m.group(2).strip(), m.group(3).strip()
        if url in vistos:
            continue
        vistos.add(url)
        salida.append({"url": BASE + url, "local": local, "visitante": visitante})
    return salida


def _numeros(bloque: str) -> list[float]:
    """Cuotas plausibles de un bloque de texto."""
    salida = []
    for t in re.findall(r"(?<![\d.])(\d{1,4}(?:\.\d{1,2})?)(?![\d.])", bloque):
        try:
            v = float(t)
        except ValueError:
            continue
        # Una cuota nunca baja de 1,01 ni suele pasar de 1000. Fuera de ese
        # rango son bonos, tamaños de imagen o años.
        if 1.01 <= v <= 1000:
            salida.append(v)
    return salida


def _bloque(texto: str, titulo: str, siguiente: list[str]) -> str:
    i = texto.find(titulo)
    if i < 0:
        return ""
    fin = len(texto)
    for s in siguiente:
        j = texto.find(s, i + len(titulo))
        if 0 < j < fin:
            fin = j
    return texto[i:fin]


def leer_1x2(texto: str) -> dict | None:
    """
    Mediana de las cuotas 1X2 de todas las casas.

    La mediana y no la mejor cuota: la mejor la pone la casa más agresiva o
    la que se ha equivocado, y aquí interesa el consenso del mercado.

    El bloque tiene esta forma, una fila por casa:

        Full Time Result
        Bookmaker / 1 / X / 2 / Bonus up to     <- cabecera
        40 / 10 / 1.02                          <- una casa
        65 / 21 / 1.02                          <- otra
        ...

    Se empieza a leer DESPUÉS de "Bonus up to" porque el "1" y el "2" de la
    cabecera son números y se colarían como si fueran cuotas.
    """
    bloque = _bloque(texto, "Full Time Result", ["Half Time Result", "Under/Over"])
    if not bloque:
        return None

    # Todo lo anterior a la cabecera es rótulo, no dato.
    corte = bloque.find("Bonus up to")
    if corte < 0:
        corte = bloque.find("BONUS UP TO")
    if corte >= 0:
        bloque = bloque[corte + len("Bonus up to"):]

    nums = _numeros(bloque)
    filas = [tuple(nums[i:i + 3]) for i in range(0, len(nums) - 2, 3)]
    filas = [f for f in filas if len(f) == 3]

    # Cada fila tiene que ser un mercado coherente: sus inversos suman algo
    # por encima de 1 (el margen de la casa) y no disparatado. Así se cae
    # sola cualquier fila mal alineada.
    validas = []
    for c1, cx, c2 in filas:
        if min(c1, cx, c2) < 1.01 or max(c1, cx, c2) > 1000:
            continue
        m = 1 / c1 + 1 / cx + 1 / c2 - 1
        if 0.0 <= m <= 0.30:
            validas.append((c1, cx, c2))

    if len(validas) < 3:
        return None

    c1 = statistics.median(f[0] for f in validas)
    cx = statistics.median(f[1] for f in validas)
    c2 = statistics.median(f[2] for f in validas)

    margen = 1 / c1 + 1 / cx + 1 / c2 - 1
    if not (MARGEN_MINIMO <= margen <= 0.25):
        return None

    return {
        "cuota_local": round(c1, 3),
        "cuota_empate": round(cx, 3),
        "cuota_visitante": round(c2, 3),
        "casas": len(validas),
        "margen": round(margen, 4),
    }


def leer_pleno(texto: str) -> dict | None:
    """
    Goles de cada equipo (0/1/2/M) a partir del marcador exacto.

    Se suma la matriz de marcadores por filas y por columnas. Si el margen
    se dispara es que la página se ha dejado marcadores sin listar —pasa, y
    el 0-0 es el que más falta— y entonces se devuelve None: un Pleno mal
    calculado es peor que ninguno.
    """
    # No se corta por "On ": esa palabra puede aparecer dentro del bloque y
    # dejaría fuera marcadores, que es justo lo que hay que evitar. Cortar de
    # más es inofensivo (el texto sobrante no tiene marcadores) y cortar de
    # menos sesga la distribución.
    bloque = _bloque(texto, "Correct Score", ["Bookmaker review", "FAQ"])[:20000]
    if not bloque:
        return None

    pares = [
        (int(m.group(1)), int(m.group(2)), float(m.group(3)))
        for m in re.finditer(r"(\d)-(\d)\s*\n\s*([\d.]+)", bloque)
    ]
    pares = [(l, v, c) for l, v, c in pares if 1.01 <= c <= 2000]
    if len(pares) < 20:
        return None

    suma = sum(1 / c for _, _, c in pares)
    margen = suma - 1
    if not (MARGEN_MINIMO <= margen <= MARGEN_MAXIMO_PLENO):
        return None

    def cat(g: int) -> str:
        return "M" if g >= 3 else str(g)

    local = {"0": 0.0, "1": 0.0, "2": 0.0, "M": 0.0}
    visitante = {"0": 0.0, "1": 0.0, "2": 0.0, "M": 0.0}
    for l, v, c in pares:
        p = (1 / c) / suma
        local[cat(l)] += p
        visitante[cat(v)] += p

    def fmt(d: dict) -> dict:
        return {
            "p0": round(d["0"], 6), "p1": round(d["1"], 6),
            "p2": round(d["2"], 6), "pm": round(d["M"], 6),
        }

    return {
        "local": fmt(local),
        "visitante": fmt(visitante),
        "marcadores": len(pares),
        "margen": round(margen, 4),
        "tiene_0_0": any(l == 0 and v == 0 for l, v, _ in pares),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Captura cuotas de Liga F.")
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    p.add_argument("--franja", default="T-24", help="franja temporal de la captura")
    p.add_argument(
        "--sin-pleno", action="store_true",
        help="no leer el marcador exacto (más rápido)",
    )
    args = p.parse_args()

    api = ApiIngesta()

    pendientes = api.contexto_cuotas(
        dias_futuro=10, retro_horas=6, solo_sin_cuotas=False, limite=60,
        genero="FEMENINO",
    )
    print(f"Partidos de Liga F en la base: {len(pendientes)}")
    if not pendientes:
        print("No hay partidos próximos. Nada que capturar.")
        return 0

    print("Leyendo el comparador...")
    anunciados = partidos_del_listado(pedir(LISTADO))
    print(f"  {len(anunciados)} partidos con cuotas publicadas\n")

    if not anunciados:
        print(
            "El comparador no ha devuelto ningún partido. O no hay jornada, "
            "o ha cambiado el HTML de la página."
        )
        return 1

    # La casilla 15 de la jornada en curso: es la única cuyo marcador exacto
    # interesa, porque es la que se juega a goles. Si el Pleno de esta
    # jornada no es un partido de Liga F —lo normal— no habrá nada que
    # guardar, y eso no es un error.
    casilla15 = None
    jornada_actual = None
    try:
        d = api.contexto_dashboard()
        jornada_actual = d.get("jornada")
        for c in d.get("casillas", []):
            if int(c["posicion"]) == 15:
                casilla15 = c
    except Exception as exc:  # noqa: BLE001
        print(f"  (no se pudo leer la jornada en curso: {exc})")

    if casilla15:
        print(f"Casilla 15 de la jornada: {casilla15['local']} - {casilla15['visitante']}\n")

    guardadas = 0
    plenos = 0
    sin_emparejar = []

    for cand in anunciados:
        nl, nv = normalizar(cand["local"]), normalizar(cand["visitante"])

        mejor, score = None, 0.0
        for p_ in pendientes:
            s = (parecido(nl, normalizar(p_["equipo_local"]))
                 + parecido(nv, normalizar(p_["equipo_visitante"]))) / 2
            if s > score:
                score, mejor = s, p_

        if not mejor or score < UMBRAL:
            sin_emparejar.append((cand, mejor, score))
            continue

        time.sleep(PAUSA)
        try:
            texto = a_texto(pedir(cand["url"]))
        except RuntimeError as exc:
            print(f"  {cand['local']} - {cand['visitante']}: {exc}")
            continue

        cuotas = leer_1x2(texto)
        if not cuotas:
            print(f"  {cand['local']} - {cand['visitante']}: sin 1X2 legible")
            continue

        inv = [1 / cuotas["cuota_local"], 1 / cuotas["cuota_empate"],
               1 / cuotas["cuota_visitante"]]
        s = sum(inv)
        print(
            f"  [{score:.2f}] {cand['local'][:20]:<20} - {cand['visitante'][:20]:<20} "
            f"{cuotas['cuota_local']}/{cuotas['cuota_empate']}/{cuotas['cuota_visitante']}"
            f"  →  {inv[0]/s*100:.0f}/{inv[1]/s*100:.0f}/{inv[2]/s*100:.0f}"
            f"  ({cuotas['casas']} casas, margen {cuotas['margen']:.1%})"
        )

        if not args.dry_run:
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
                "fuente": "sportytrader",
            })
            guardadas += 1

        if args.sin_pleno:
            continue

        # ¿Es este partido el Pleno de la jornada?
        es_pleno = (
            casilla15 is not None
            and jornada_actual is not None
            and casilla15.get("partido_id") is not None
            and int(casilla15["partido_id"]) == int(mejor["partido_id"])
        )
        if not es_pleno:
            continue

        pleno = leer_pleno(texto)
        if not pleno:
            print("      es la casilla 15, pero el Pleno se descarta: "
                  "faltan marcadores o el margen es anómalo")
            continue

        l, v = pleno["local"], pleno["visitante"]
        print(
            f"      PLENO  local {l['p0']:.0%}/{l['p1']:.0%}/{l['p2']:.0%}/{l['pm']:.0%}"
            f"  visitante {v['p0']:.0%}/{v['p1']:.0%}/{v['p2']:.0%}/{v['pm']:.0%}"
            f"  ({pleno['marcadores']} marcadores, margen {pleno['margen']:.0%})"
        )
        plenos += 1

        if not args.dry_run:
            api.guardar_pleno({
                "numero_jornada": int(jornada_actual["numero"]),
                "etiqueta_temporada": jornada_actual["temporada"],
                "fuente": "MERCADO",
                "tipo": args.franja,
                "calidad": "sportytrader",
                "local": l,
                "visitante": v,
            })

    if sin_emparejar:
        print(f"\n  Sin emparejar ({len(sin_emparejar)}):")
        for c, mejor, s in sin_emparejar:
            aprox = f" — lo más parecido: {mejor['equipo_local']} ({s:.2f})" if mejor else ""
            print(f"    {c['local']} - {c['visitante']}{aprox}")

    print(f"\n{guardadas} cuotas guardadas, {plenos} plenos legibles.")
    if args.dry_run:
        print("DRY RUN: no se ha escrito nada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
