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
2. Entrena el Elo en orden cronológico y calibra la probabilidad de empate
   por tramo de diferencia de valoración.
3. Empareja las casillas de la jornada con los equipos por nombre.
4. Guarda las probabilidades como fuente MODELO_PROPIO.

Antes de guardar mide su propio acierto sobre una parte del histórico que no
ha usado para entrenar. Si el modelo no supera al azar informado, avisa: es
preferible no tener columna a tener una que engañe.

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
from difflib import SequenceMatcher

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_client import ApiIngesta  # noqa: E402

from modelado.elo import ModeloElo, Partido, dividir_temporal, evaluar  # noqa: E402


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

# Cuánto tiene que mejorar el modelo al baseline (predecir siempre la
# frecuencia media de 1/X/2) para que merezca publicarse su pronóstico.
# Medido sobre validación y por competición separada, porque el modelo no
# rinde igual en todas:
#
#     Liga F             66,3% acierto   mejora +0,1590
#     LaLiga             49,4%           mejora +0,0232
#     Segunda División   40,8%           mejora -0,0274
#
# En Segunda el modelo es peor que no tener modelo, así que ahí no se
# guarda: una columna que empeora la decisión no es información, es ruido
# con aspecto de dato.
MEJORA_MINIMA = 0.01


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


def a_partidos(filas: list[dict]) -> list[Partido]:
    salida = []
    for f in filas:
        try:
            salida.append(Partido(
                fecha=f["fecha_hora_inicio"],
                local=f["equipo_local"],
                visitante=f["equipo_visitante"],
                goles_local=int(f["goles_local"]),
                goles_visitante=int(f["goles_visitante"]),
                signo=f["signo"],
                competicion=f.get("competicion", ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return salida


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
    partidos = a_partidos(filas)
    print(f"  {len(partidos)} partidos con resultado")

    comps: dict[str, int] = {}
    for pt in partidos:
        comps[pt.competicion] = comps.get(pt.competicion, 0) + 1
    for c, n in sorted(comps.items(), key=lambda x: -x[1]):
        print(f"    {c or '(sin competición)'}: {n}")

    if len(partidos) < 300:
        print("\nHay muy pocos partidos para entrenar. No se guarda nada.")
        return 1

    # --- Validación antes de usarlo -------------------------------------
    #
    # Se mide sobre partidos posteriores a los de entrenamiento, nunca
    # repartidos al azar: predecir un partido de 2023 habiendo visto los de
    # 2025 daría un acierto que no se repetiría en la realidad.

    train, resto = dividir_temporal(partidos, fraccion_train=0.80)
    print(f"\nValidando: {len(train)} para entrenar, {len(resto)} para comprobar")

    prueba = ModeloElo(k=20.0, ventaja_local=0.0)
    prueba.entrenar(train)

    competiciones = sorted({p_.competicion for p_ in resto if p_.competicion})
    aptas: set[str] = set()

    print(f"  {'competición':<20} {'n':>5} {'acierto':>8} {'mejora':>9}")
    for comp in competiciones + [None]:
        sub = resto if comp is None else [x for x in resto if x.competicion == comp]
        if len(sub) < 30:
            continue
        r = evaluar(prueba, sub)
        apta = comp is not None and r["mejora_sobre_base"] >= MEJORA_MINIMA
        if apta:
            aptas.add(comp)
        marca = "  se publica" if apta else ("" if comp is None else "  se descarta")
        print(
            f"  {(comp or 'TODAS'):<20} {r['n']:>5} {r['acierto']:>7.1%} "
            f"{r['mejora_sobre_base']:>+9.4f}{marca}"
        )

    if not aptas:
        print(
            "\nEl modelo no mejora al baseline en ninguna competición. "
            "No se guarda nada: una columna que engaña es peor que ninguna."
        )
        return 1

    print(f"\n  Se publicará solo: {', '.join(sorted(aptas))}")

    # --- Modelo definitivo, ya con todo el histórico ---------------------

    modelo = ModeloElo(k=20.0, ventaja_local=0.0)
    modelo.entrenar(partidos)

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
    for pt in partidos:
        for eq in (pt.local, pt.visitante):
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

        pr = modelo.predecir(local, visitante)
        casillas.append({
            "posicion": pos,
            "p1": round(pr["1"], 6),
            "px": round(pr["X"], 6),
            "p2": round(pr["2"], 6),
        })

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
