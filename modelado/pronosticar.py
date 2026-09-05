"""
Entrena el modelo Elo con el histórico y pronostica la jornada actual.

Qué resuelve
------------

`modelado/elo.py` era solo una librería: definía el modelo pero nadie lo
ejecutaba, así que el pronóstico propio no existía en ninguna tabla y el
panel no podía enseñarlo. Este script cierra ese hueco.

Cómo funciona
-------------

1. Descarga los partidos con resultado (4.400 a día de hoy: LaLiga, Segunda
   y Liga F).
2. Describe cada uno con lo que se sabía antes de jugarlo: valoración Elo,
   forma reciente, goles, descanso y enfrentamientos directos (ver
   modelado/caracteristicas.py).
3. Entrena una regresión logística sobre esas características.
4. Empareja las casillas de la jornada con los equipos por nombre y
   pronostica.
5. Guarda las probabilidades como fuente MODELO_PROPIO.

Antes de guardar mide su propio acierto sobre partidos posteriores a los de
entrenamiento, competición por competición, y solo publica donde no empeora
al baseline. Es preferible no tener columna a tener una que engañe.

Por qué un solo Elo para las tres competiciones
-----------------------------------------------

LaLiga y Segunda comparten equipos por ascensos y descensos, así que sus
valoraciones tienen que estar en la misma escala. Liga F no se cruza con
ninguna de las dos, pero eso no estorba: al no haber partidos entre ellas,
sus valoraciones simplemente forman una escala paralela.

Uso
---

    python -m modelado.pronosticar --dry-run
    python -m modelado.pronosticar --temporada 2026-27 --jornada 4
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_client import ApiIngesta  # noqa: E402

import numpy as np  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from modelado import caracteristicas  # noqa: E402
from modelado.elo import Partido  # noqa: E402
from modelado.goles import CATEGORIAS, ModeloGoles  # noqa: E402


# Los mismos alias que usa el vinculador de boletos: el boleto abrevia de una
# forma y nucleo_equipos nombra de otra.
RUIDO = {
    "fc", "cf", "cd", "rcd", "ud", "sd", "rc", "ca", "ce", "ad", "club",
    "de", "futbol", "balompie", "femenino", "femeni", "women", "united",
}

ALIAS = {
    "ath club": "athletic", "athletic bilbao": "athletic",
    "racing s": "racing", "racing santander": "racing",
    "r racing": "racing", "real racing santander": "racing",
    "deportivo": "deportivo coruna", "rc deportivo": "deportivo coruna",
    "la coruna": "deportivo coruna", "d coruna": "deportivo coruna",
    "celta vigo": "celta", "espanyol barcelona": "espanyol",
    "r sociedad b": "real sociedad b", "sociedad b": "real sociedad b",
    "r madrid": "real madrid", "at madrid": "atletico madrid",
    # En nucleo_equipos el club figura como "Real Sporting", sin "Gijón".
    # Mapearlo a "sporting gijon" lo dejaba a 0,59 de su propio registro
    # mientras "Sporting Club Huelva" puntuaba 0,62: bajar el umbral habría
    # emparejado con el equipo equivocado.
    "sp gijon": "real sporting", "sporting": "real sporting",
    "sporting gijon": "real sporting",
    "logrono": "logrono united",
}

UMBRAL = 0.72

# Un pronóstico se publica si no empeora al baseline (predecir siempre la
# frecuencia media de 1/X/2). El listón está en cero y no más arriba porque
# una señal débil pero honesta sigue siendo información; lo inaceptable es
# publicar una que estorbe.
#
# Medido por competición, que es donde el modelo rinde muy distinto:
#
#     competición        solo Elo            con características
#     LaLiga             49,4%  +0,0232      50,8%  +0,0522
#     Segunda División   40,8%  -0,0274      46,6%  +0,0038
#     Liga F             66,3%  +0,1590      63,0%  +0,2334
#
# Segunda es la razón de ser de modelado/caracteristicas.py: con Elo a secas
# hacía daño, y con forma reciente, descanso y enfrentamientos directos deja
# de hacerlo. Sigue siendo la liga más difícil de predecir, y eso no es un
# defecto del modelo sino de la competición: está muy igualada.
MEJORA_MINIMA = 0.0


def normalizar(nombre: str) -> str:
    nombre = re.sub(r"\((?:F|M)\)", " ", nombre, flags=re.I)
    txt = unicodedata.normalize("NFKD", nombre)
    txt = "".join(c for c in txt if not unicodedata.combining(c)).lower()
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)
    palabras = [p for p in txt.split() if p]
    filtradas = [p for p in palabras if p not in RUIDO]
    base = " ".join(filtradas) if filtradas else " ".join(palabras)
    return ALIAS.get(base, base)


def parecido(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def descargar_resultados(api: ApiIngesta) -> list[dict]:
    """Todos los partidos con resultado, paginando."""
    todos: list[dict] = []
    offset = 0
    while True:
        d = api.contexto_resultados(limite=1000, offset=offset)
        items = d.get("items", [])
        todos += items
        offset += len(items)
        if not d.get("hay_mas") or not items:
            break
    return todos


def main() -> int:
    p = argparse.ArgumentParser(
        description="Entrena el Elo y pronostica una jornada."
    )
    p.add_argument("--temporada", default="", help="p.ej. 2026-27")
    p.add_argument("--jornada", type=int, default=0)
    p.add_argument("--tipo", default="T-24", help="franja de la captura")
    p.add_argument("--dry-run", action="store_true", help="no guarda, solo informa")
    args = p.parse_args()

    api = ApiIngesta()

    print("Descargando histórico...")
    filas = descargar_resultados(api)
    X, y, meta, estado_final = caracteristicas.construir(filas)
    print(f"  {len(X)} partidos con resultado")

    comps: dict[str, int] = {}
    for c, *_ in meta:
        comps[c] = comps.get(c, 0) + 1
    for c, n in sorted(comps.items(), key=lambda x: -x[1]):
        print(f"    {c or '(sin competición)'}: {n}")

    if len(X) < 300:
        print("\nHay muy pocos partidos para entrenar. No se guarda nada.")
        return 1

    # --- Validación antes de usarlo -------------------------------------
    #
    # El corte es por fecha, nunca al azar: entrenar con partidos de 2026 y
    # evaluar sobre 2023 daría un acierto que no se repetiría en la realidad,
    # porque en la realidad el futuro no se conoce.

    Xa = np.array(X)
    ya = np.array(y)
    corte = int(len(Xa) * 0.80)

    print(f"\nValidando: {corte} para entrenar, {len(Xa) - corte} para comprobar")

    prueba = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, C=0.5),
    )
    prueba.fit(Xa[:corte], ya[:corte])
    proba = prueba.predict_proba(Xa[corte:])
    real = ya[corte:]
    meta_test = meta[corte:]

    frecuencias = np.bincount(ya[:corte], minlength=3) / corte
    aptas: set[str] = set()

    print(f"  {'competición':<20} {'n':>5} {'acierto':>8} {'mejora':>9}")
    for comp in sorted({c for c, *_ in meta_test if c}) + [None]:
        idx = [
            i for i, (c, *_) in enumerate(meta_test)
            if comp is None or c == comp
        ]
        if len(idx) < 30:
            continue
        p = proba[idx]
        t = real[idx]
        acierto = float((p.argmax(1) == t).mean())
        ll = float(-np.mean(np.log(np.clip(p[np.arange(len(t)), t], 1e-9, 1))))
        base = float(-np.mean(np.log(frecuencias[t])))
        mejora = base - ll

        apta = comp is not None and mejora >= MEJORA_MINIMA
        if apta:
            aptas.add(comp)
        marca = "  se publica" if apta else ("" if comp is None else "  se descarta")
        print(
            f"  {(comp or 'TODAS'):<20} {len(idx):>5} {acierto:>7.1%} "
            f"{mejora:>+9.4f}{marca}"
        )

    if not aptas:
        print(
            "\nEl modelo no mejora al baseline en ninguna competición. "
            "No se guarda nada: una columna que engaña es peor que ninguna."
        )
        return 1

    print(f"\n  Se publicará: {', '.join(sorted(aptas))}")

    # --- Modelo definitivo, ya con todo el histórico ---------------------

    modelo = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, C=0.5),
    )
    modelo.fit(Xa, ya)

    # --- La jornada -------------------------------------------------------

    datos = api.contexto_dashboard(
        temporada=args.temporada or None,
        numero_jornada=args.jornada or None,
    )
    jornada = datos.get("jornada")
    if not jornada:
        print("\nNo hay ninguna jornada cargada.")
        return 1

    print(f"\nJornada {jornada['numero']} · {jornada['temporada']}")

    # Índice de equipos conocidos por nombre normalizado.
    conocidos = {}
    for eq in estado_final.historial:
        conocidos.setdefault(normalizar(eq), eq)

    def buscar(nombre: str) -> str | None:
        """
        El equipo de la base que corresponde a este nombre del boleto.

        Devuelve None ante la duda. Un emparejamiento equivocado es peor que
        ninguno: metería en el panel el pronóstico de otro equipo sin que
        nada lo delate. Por eso se rechaza también cuando dos candidatos
        puntúan casi igual, como "Real Sporting" y "Sporting Club Huelva".
        """
        n = normalizar(nombre)
        if n in conocidos:
            return conocidos[n]

        puntuados = sorted(
            ((parecido(n, k), v) for k, v in conocidos.items()),
            reverse=True,
        )
        if not puntuados or puntuados[0][0] < UMBRAL:
            return None
        if len(puntuados) > 1 and puntuados[0][0] - puntuados[1][0] < 0.08:
            return None
        return puntuados[0][1]

    casillas = []
    sin_equipo = []
    descartadas = []

    for c in sorted(datos["casillas"], key=lambda x: int(x["posicion"])):
        pos = int(c["posicion"])
        if pos == 15:
            continue

        # Solo las competiciones donde el modelo demostró aportar.
        if (c.get("competicion") or "") not in aptas:
            descartadas.append((pos, c["local"], c["visitante"], c.get("competicion") or "?"))
            continue

        local = buscar(c["local"])
        visitante = buscar(c["visitante"])

        if not local or not visitante:
            sin_equipo.append((pos, c["local"], c["visitante"]))
            continue

        # Las características se calculan con el estado tras todo el
        # histórico, que es justo lo que se sabe al llegar la jornada.
        cuando = datetime.now()
        if c.get("fecha_hora_inicio"):
            try:
                cuando = datetime.fromisoformat(c["fecha_hora_inicio"])
            except ValueError:
                pass

        vector = estado_final.caracteristicas(local, visitante, cuando)
        p1, px, p2 = modelo.predict_proba(np.array([vector]))[0]

        casillas.append({
            "posicion": pos,
            "p1": round(float(p1), 6),
            "px": round(float(px), 6),
            "p2": round(float(p2), 6),
        })
        pr = {"1": p1, "X": px, "2": p2}

        mer = c.get("mercado")
        cmp_ = ""
        if mer:
            cmp_ = (f"   mercado {mer['p1']*100:.0f}/{mer['px']*100:.0f}/{mer['p2']*100:.0f}")
        print(
            f"  {pos:>2}. {c['local'][:16]:<16} - {c['visitante'][:16]:<16}"
            f"  {pr['1']*100:>3.0f}/{pr['X']*100:>3.0f}/{pr['2']*100:>3.0f}{cmp_}"
        )

    if descartadas:
        print(
            f"\n  Fuera ({len(descartadas)}): el modelo no supera al baseline "
            "en su competición, y ahí manda el mercado."
        )
        for pos, l, v, comp in descartadas:
            print(f"    {pos:>2}. {l} - {v}  ({comp})")

    if sin_equipo:
        print(f"\n  Sin histórico ({len(sin_equipo)}), quedan sin pronóstico:")
        for pos, l, v in sin_equipo:
            print(f"    {pos:>2}. {l} - {v}")

    if not casillas:
        print("\nNo se ha podido pronosticar ninguna casilla.")
        return 1

    # --- El Pleno al 15 ---------------------------------------------------
    #
    # Se juega a goles de cada equipo, no a 1/X/2, así que necesita su propio
    # modelo: Poisson con fuerzas de ataque y defensa sacadas del histórico.
    #
    # Medido sobre 881 partidos que no vio al entrenar: acierta la categoría
    # de un equipo el 37,2% de las veces y mejora al baseline en +0,0470,
    # en la línea de lo que aporta el modelo de 1X2 en LaLiga. Poco para
    # jugarse el Pleno a ciegas, suficiente para no dejar la casilla vacía.

    pleno = None
    casilla15 = next(
        (c for c in datos["casillas"] if int(c["posicion"]) == 15), None
    )
    if casilla15:
        l15 = buscar(casilla15["local"])
        v15 = buscar(casilla15["visitante"])
        if l15 and v15:
            historicos = []
            for f in filas:
                try:
                    historicos.append(Partido(
                        fecha=f["fecha_hora_inicio"],
                        local=f["equipo_local"], visitante=f["equipo_visitante"],
                        goles_local=int(f["goles_local"]),
                        goles_visitante=int(f["goles_visitante"]),
                        signo=f["signo"], competicion=f.get("competicion", ""),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue

            mg = ModeloGoles()
            mg.entrenar(historicos)
            pr15 = mg.predecir(l15, v15)

            pleno = {
                lado: {
                    "p0": round(pr15[lado]["0"], 6),
                    "p1": round(pr15[lado]["1"], 6),
                    "p2": round(pr15[lado]["2"], 6),
                    "pm": round(pr15[lado]["M"], 6),
                }
                for lado in ("local", "visitante")
            }
            print(
                f"\n  Pleno 15: {casilla15['local']} - {casilla15['visitante']}"
            )
            for lado in ("local", "visitante"):
                d = pr15[lado]
                print(
                    f"    {lado:<10} "
                    + "  ".join(f"{c}:{d[c]:>4.0%}" for c in CATEGORIAS)
                    + f"   → '{max(d, key=d.get)}'"
                )
        else:
            print("\n  Pleno 15: sin histórico de alguno de los dos equipos.")

    if args.dry_run:
        print("\nDRY RUN: no se ha guardado nada.")
        return 0

    r = api.guardar_pronostico({
        "numero_jornada": int(jornada["numero"]),
        "etiqueta_temporada": jornada["temporada"],
        "modelo": "elo_v1",
        "tipo": args.tipo,
        "casillas": casillas,
    })
    print(f"\nGuardado: {r.get('casillas')} casillas como {r.get('fuente')}")

    if pleno:
        rp = api.guardar_pleno({
            "numero_jornada": int(jornada["numero"]),
            "etiqueta_temporada": jornada["temporada"],
            "fuente": "MODELO_PROPIO",
            "tipo": args.tipo,
            "calidad": "poisson_v1",
            "local": pleno["local"],
            "visitante": pleno["visitante"],
        })
        print(f"Pleno guardado como {rp.get('fuente')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
