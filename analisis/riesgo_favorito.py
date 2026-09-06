"""
¿Cómo de bien reparte el público, y dónde está el valor?

DE DÓNDE VIENE ESTE SCRIPT

Empezó preguntando si la brecha entre reputación y forma servía para algo:

    brecha = diferencia de Elo − diferencia de forma

La respuesta, medida, fue que no. Tres veces que no:

  A. Como característica del modelo empeora el log-loss, y no por falta de
     muestra: la interacción se activa en 962 de 4.407 partidos.
  B. Controlando por la calidad del favorito, la señal existe pero al revés.
  C. El público no persigue la racha.

Lo que sí apareció por el camino es más útil que la idea original, y es lo
que mide ahora este script.

EL HALLAZGO

Comparando cuánto varía cada uno entre partidos igualados y partidos claros:

    lo que de verdad ocurre    varía 23 puntos
    nuestro modelo             varía 15 puntos
    el público                 varía  4 puntos

El público reparte alrededor del mismo porcentaje a su favorito tanto si el
partido está igualado como si es un paseo. Es casi ciego a la diferencia.

Si eso es cierto, da una regla en dos direcciones:

    partido igualado   el público concentra igual que si fuera claro, así
                       que sobrejuega su favorito → valor en los otros dos
    favorito claro     el público no lo juega lo suficiente → valor en él

POR QUÉ ESTA VERSIÓN MIDE LOS TRES SIGNOS

Las versiones anteriores tomaban el signo MÁS jugado por el público y lo
comparaban con el modelo o con el resultado. Eso está sesgado por
construcción: elegir el máximo de una fuente y compararlo con otra siempre
favorece a la primera. Es el sesgo del argmax, y explicaba buena parte de
los números absolutos que salían — aquel «+18,8% de media» no era real.

Midiendo los tres signos de cada casilla no se elige nada, así que no hay
sesgo. Los porcentajes de una casilla suman 100 y la casilla entra entera.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.api_client import ApiIngesta          # noqa: E402
from modelado import caracteristicas                  # noqa: E402

# El vinculador semanal ya resuelve que el boleto escriba 'AT.MADRID' donde
# la tabla pone 'Atlético de Madrid': tiene tabla de alias, filtro de
# palabras de relleno y un umbral calibrado. Escribir otra versión aquí fue
# un error que costó una ejecución entera: acertaba 2 de 8.
from mercado.vincular_boleto import normalizar, parecido, UMBRAL  # noqa: E402

SIGNOS = ("1", "X", "2")


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

def descargar_resultados(api: ApiIngesta) -> list[dict]:
    """Todos los partidos con resultado, paginando."""
    filas: list[dict] = []
    offset = 0
    while True:
        d = api.contexto_resultados(limite=1000, offset=offset)
        lote = d.get("items", [])
        filas.extend(lote)
        if not d.get("hay_mas") or not lote:
            break
        offset += len(lote)
    return filas


def clave(fecha: str, local: str, visitante: str) -> str:
    """Identificador de un partido: día más nombres recortados."""
    def limpio(s: str) -> str:
        s = (s or "").lower()
        for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"),
                     ("ú", "u"), ("ñ", "n"), (".", ""), (" ", ""), ("-", "")):
            s = s.replace(a, b)
        return s[:6]
    return f"{(fecha or '')[:10]}|{limpio(local)}|{limpio(visitante)}"


def temporada_de(fecha: str) -> str:
    """La temporada de una fecha. En España no se juega en julio."""
    d = datetime.fromisoformat(fecha)
    a = d.year if d.month >= 7 else d.year - 1
    return f"{a}-{str(a + 1)[2:]}"


def indexar(filas: list[dict]) -> dict:
    """
    Partidos por temporada y par de equipos normalizado.

    La temporada desambigua: dentro de una, un enfrentamiento con ese local
    y ese visitante ocurre una sola vez. Hace falta porque las jornadas
    viejas del boleto nunca se vincularon con la tabla de partidos y llegan
    sin fecha y sin partido_id.
    """
    idx: dict = {}
    for f in filas:
        try:
            t = temporada_de(f["fecha_hora_inicio"])
            k = clave(f["fecha_hora_inicio"], f["equipo_local"],
                      f["equipo_visitante"])
        except (KeyError, TypeError, ValueError):
            continue
        par = (normalizar(f.get("equipo_local", "")),
               normalizar(f.get("equipo_visitante", "")))
        idx.setdefault(t, {}).setdefault(par, []).append(k)
    return idx


def emparejar(casilla: dict, temporada: str, idx: dict) -> str | None:
    """
    El partido de una casilla, dentro de su temporada.

    Se exige que casen los DOS equipos: con uno solo, 'Atlético de Madrid' y
    'Real Madrid' podrían confundirse — su parecido es 0,69, no lejos del
    umbral de 0,72.
    """
    dela = idx.get(temporada)
    if not dela:
        return None
    par = (normalizar(casilla.get("local") or ""),
           normalizar(casilla.get("visitante") or ""))
    exacto = dela.get(par)
    if exacto and len(exacto) == 1:
        return exacto[0]

    mejor, mejor_p = None, 0.0
    for (nl, nv), claves in dela.items():
        if len(claves) != 1:
            continue
        p = min(parecido(par[0], nl), parecido(par[1], nv))
        if p > mejor_p:
            mejor, mejor_p = claves[0], p
    return mejor if mejor_p >= UMBRAL else None


def rasgos(filas: list[dict]) -> dict[str, tuple[float, float]]:
    """
    Elo y forma de cada partido, con lo que se sabía ANTES de jugarlo.

    Se recorre en orden llamando a caracteristicas() antes de registrar(),
    igual que el entrenamiento. Al revés daría valores con el resultado ya
    dentro, que es la manera clásica de obtener una correlación falsa.
    """
    partidos = []
    for f in filas:
        try:
            partidos.append((
                datetime.fromisoformat(f["fecha_hora_inicio"]),
                f["equipo_local"], f["equipo_visitante"],
                int(f["goles_local"]), int(f["goles_visitante"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    partidos.sort()

    estado = caracteristicas.Estado()
    i_elo = caracteristicas.NOMBRES.index("elo_diferencia")
    i_forma = caracteristicas.NOMBRES.index("forma_diferencia")
    salida: dict[str, tuple[float, float]] = {}

    for dt, loc, vis, gl, gv in partidos:
        v = estado.caracteristicas(loc, vis, dt)
        salida[clave(dt.isoformat(), loc, vis)] = (
            float(v[i_elo]), float(v[i_forma])
        )
        estado.registrar(loc, vis, dt, gl, gv)
    return salida


def probabilidades_limpias(filas: list[dict]) -> dict[str, list[float]]:
    """
    Lo que el modelo habría dado a cada partido SIN haberlo visto.

    Se entrena por bloques cronológicos: cada quinto se predice con un
    modelo que solo ha visto los anteriores. Con un modelo entrenado con
    todo, las probabilidades estarían contaminadas por el resultado que se
    quiere explicar.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, _, _ = caracteristicas.construir(filas)
    Xa, ya = np.array(X), np.array(y)

    orden = []
    for f in filas:
        try:
            orden.append((datetime.fromisoformat(f["fecha_hora_inicio"]),
                          f["equipo_local"], f["equipo_visitante"]))
        except (KeyError, TypeError, ValueError):
            continue
    orden.sort()

    salida: dict[str, list[float]] = {}
    n = len(Xa)
    bloque = n // 5
    for i in range(1, 5):
        ini, fin = i * bloque, ((i + 1) * bloque if i < 4 else n)
        m = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=2000, C=0.5))
        m.fit(Xa[:ini], ya[:ini])
        for j, p in zip(range(ini, fin), m.predict_proba(Xa[ini:fin])):
            if j < len(orden):
                dt, loc, vis = orden[j]
                salida[clave(dt.isoformat(), loc, vis)] = [float(x) for x in p]
    return salida


def terna(d: dict | None) -> list[float] | None:
    """Los tres porcentajes normalizados para que sumen 1."""
    if not d:
        return None
    try:
        v = [float(d["p1"]), float(d["px"]), float(d["p2"])]
    except (KeyError, TypeError, ValueError):
        return None
    t = sum(v)
    return [x / t for x in v] if t > 0 else None


# ---------------------------------------------------------------------------
# Medidas
# ---------------------------------------------------------------------------

def pendiente(p: np.ndarray, ocurre: np.ndarray) -> tuple[float, float]:
    """
    Cuánto sigue una fuente a la realidad, con su intervalo al 90%.

    Es la pendiente de ajustar `ocurre` sobre `p`, y va al revés de lo que
    parece a primera vista. Comprobado con datos donde se conoce la
    respuesta:

        fuente calibrada (dice = verdad)          pendiente 0,98
        fuente comprimida (varía 4 veces menos)   pendiente 3,92
        fuente exagerada (varía 1,6 veces más)    pendiente 0,61

    Tiene sentido: si una fuente comprime, entre dos porcentajes suyos muy
    parecidos hay realidades muy distintas, así que hace falta una pendiente
    grande para ir de una a otra.

        pendiente ≈ 1   la fuente sigue a la realidad
        pendiente > 1   COMPRIMIDA: no distingue lo suficiente entre
                        partidos claros e igualados
        pendiente < 1   EXAGERADA: separa más de lo que la realidad separa

    Interesa más que el acierto. Una fuente puede acertar bastante y aun así
    repartir sus porcentajes casi igual en todos los partidos, y es
    justamente ahí donde aparece el valor.
    """
    if len(p) < 50 or p.std() == 0:
        return (float("nan"), float("nan"))
    b = float(np.polyfit(p, ocurre, 1)[0])
    rng = np.random.default_rng(17)
    muestras = []
    for _ in range(1000):
        i = rng.integers(0, len(p), len(p))
        if p[i].std() == 0:
            continue
        muestras.append(np.polyfit(p[i], ocurre[i], 1)[0])
    if not muestras:
        return (b, float("nan"))
    lo, hi = np.percentile(muestras, [5, 95])
    return (b, float(hi - lo) / 2)


def calibracion(p: np.ndarray, ocurre: np.ndarray, titulo: str) -> None:
    """Lo que dice una fuente contra lo que pasa, por tramos."""
    print(f"\n  {titulo}")
    print(f"  {'tramo':<14} {'n':>6} {'dice':>8} {'ocurre':>8} "
          f"{'error':>8} {'valor':>7}")
    bordes = [0.0, .10, .20, .30, .40, .50, .60, .70, 1.01]
    for lo, hi in zip(bordes[:-1], bordes[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum() < 25:
            continue
        dice = float(p[m].mean())
        pasa = float(ocurre[m].mean())
        # El valor es lo que de verdad decide el boleto: cuántas veces
        # ocurre por cada vez que se juega. Por encima de 1, el signo está
        # infrajugado y paga más de lo que le corresponde.
        val = pasa / dice if dice > 0 else float("nan")
        print(f"  {f'{lo:.0%}-{hi:.0%}':<14} {int(m.sum()):>6} {dice:>7.1%} "
              f"{pasa:>7.1%} {pasa - dice:>+8.1%} {val:>7.2f}")


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Cómo reparte el público.")
    p.add_argument("--jornadas", type=int, default=0,
                   help="cuántas jornadas mirar (0 = todas)")
    args = p.parse_args()

    api = ApiIngesta()

    print("Descargando resultados...")
    filas = descargar_resultados(api)
    print(f"  {len(filas)} partidos con resultado")

    print("Elo y forma previos a cada partido...")
    rg = rasgos(filas)

    print("Probabilidades del modelo fuera de muestra...")
    modelo = probabilidades_limpias(filas)
    print(f"  {len(modelo)} partidos con probabilidad no contaminada")

    idx = indexar(filas)
    conocidos = {normalizar(f.get("equipo_local", "")) for f in filas}
    conocidos |= {normalizar(f.get("equipo_visitante", "")) for f in filas}

    # ------------------------------------------------------------------
    print("\n" + "=" * 68)
    print("INVENTARIO")
    print("=" * 68)

    años = sorted({
        (datetime.fromisoformat(f["fecha_hora_inicio"]).year
         if datetime.fromisoformat(f["fecha_hora_inicio"]).month >= 7
         else datetime.fromisoformat(f["fecha_hora_inicio"]).year - 1)
        for f in filas if f.get("fecha_hora_inicio")
    })
    temporadas = [f"{a}-{str(a + 1)[2:]}" for a in años]

    jornadas = []
    for t in temporadas:
        try:
            jornadas.extend(api.contexto_quiniela(temporada=t, limite=50))
        except Exception as exc:                     # noqa: BLE001
            print(f"    {t}: {exc}")
    if args.jornadas and len(jornadas) > args.jornadas:
        jornadas = jornadas[-args.jornadas:]
    print(f"  {len(jornadas)} jornadas")

    # Una fila por casilla y signo: tres por casilla, sin elegir ninguna.
    # Así no hay sesgo de argmax.
    lae_p: list[float] = []
    mod_p: list[float] = []
    paso: list[float] = []
    casillas_ok = 0
    fuera = 0
    no_casan = 0

    for j in jornadas:
        try:
            d = api.contexto_dashboard(
                temporada=j.get("etiqueta_temporada"),
                numero_jornada=int(j.get("numero_jornada")),
            )
        except Exception:                            # noqa: BLE001
            continue

        for c in d.get("casillas", []):
            if int(c.get("posicion", 0)) == 15:
                continue
            pr = c.get("probabilidades") or {}
            lae = terna(pr.get("LAE_CIERRE") or pr.get("LAE_ESTIMADO"))
            if not lae:
                continue
            real = c.get("signo_oficial")
            if real not in SIGNOS:
                continue

            k = emparejar(c, j.get("etiqueta_temporada") or "", idx)
            if k is None:
                # La quiniela de verano se juega con ligas extranjeras.
                # No es que el emparejamiento falle: es que esos partidos
                # no están en nuestra base, que solo sigue LaLiga, Segunda
                # y Liga F.
                nl = normalizar(c.get("local") or "")
                nv = normalizar(c.get("visitante") or "")
                if (nl and nl not in conocidos) or (nv and nv not in conocidos):
                    fuera += 1
                else:
                    no_casan += 1
                continue

            pm = modelo.get(k)
            if pm is None:
                continue

            casillas_ok += 1
            for s, signo in enumerate(SIGNOS):
                lae_p.append(lae[s])
                mod_p.append(pm[s])
                paso.append(1.0 if real == signo else 0.0)

    print(f"  {casillas_ok} casillas completas → {len(lae_p)} filas "
          "(tres signos por casilla)")
    print(f"  {fuera} casillas de competiciones que no seguimos "
          "(la quiniela de verano)")
    if no_casan:
        print(f"  {no_casan} de equipos conocidos que aun así no casan")

    if casillas_ok < 100:
        print("\n  Muy pocas casillas para concluir nada.")
        return 0

    lae = np.array(lae_p)
    mod = np.array(mod_p)
    ok = np.array(paso)

    # ------------------------------------------------------------------
    print("\n" + "=" * 68)
    print("¿CÓMO DE BIEN REPARTE CADA UNO?")
    print("=" * 68)
    print()
    print("  Los tres signos de cada casilla, sin elegir ninguno. Si una")
    print("  fuente da un 30% a un signo, ese signo debería salir el 30%")
    print("  de las veces.")

    calibracion(lae, ok, "EL PÚBLICO (porcentajes de LAE)")
    calibracion(mod, ok, "EL MODELO (fuera de muestra, como referencia)")

    b_lae, e_lae = pendiente(lae, ok)
    b_mod, e_mod = pendiente(mod, ok)
    print()
    print("  SENSIBILIDAD: cuánto hay que moverse en la realidad para que la")
    print("  fuente se mueva un punto. Uno es perfecto. POR ENCIMA de uno")
    print("  está comprimida (no distingue lo bastante); por debajo, exagera.")
    print()
    print(f"    público   {b_lae:.2f} ± {e_lae:.2f}")
    print(f"    modelo    {b_mod:.2f} ± {e_mod:.2f}")
    print()
    if not np.isnan(e_lae) and b_lae - e_lae > 1.15:
        print("  El público está COMPRIMIDO: reparte parecido tanto si el")
        print("  partido está igualado como si hay favorito claro. Ahí es")
        print("  donde vive el valor, y la tabla de arriba dice en qué")
        print("  tramos está.")
    elif not np.isnan(e_lae) and b_lae + e_lae < 0.85:
        print("  El público EXAGERA las diferencias: separa más de lo que la")
        print("  realidad separa.")
    else:
        print("  El público sigue a la realidad razonablemente bien. La")
        print("  hipótesis de la compresión no se sostiene.")

    # Quien está más cerca de 1 sigue mejor a la realidad, venga por arriba
    # o por abajo. Comparar los números en crudo diría que 0,42 es mejor que
    # 1,30, y es al revés.
    if not np.isnan(e_mod) and not np.isnan(e_lae):
        d_lae = abs(b_lae - 1.0)
        d_mod = abs(b_mod - 1.0)
        print()
        if d_mod < d_lae:
            print(f"  El modelo sigue a la realidad mejor que el público")
            print(f"  ({d_mod:.2f} de distancia al ideal frente a {d_lae:.2f}).")
            print("  Esa diferencia es lo que el sistema puede aprovechar.")
        else:
            print(f"  El público sigue a la realidad mejor que nuestro modelo")
            print(f"  ({d_lae:.2f} frente a {d_mod:.2f}). Conviene saberlo.")

    # ------------------------------------------------------------------
    print("\n" + "=" * 68)
    print("LA IDEA ORIGINAL (cerrada, se deja como registro)")
    print("=" * 68)

    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, _, _ = caracteristicas.construir(filas)
    Xa, ya = np.array(X), np.array(y)
    elo = Xa[:, caracteristicas.NOMBRES.index("elo_diferencia")]
    fo = Xa[:, caracteristicas.NOMBRES.index("forma_diferencia")]
    corte = int(len(Xa) * 0.80)

    def perdida(datos):
        m = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=2000, C=0.5))
        m.fit(datos[:corte], ya[:corte])
        pp = m.predict_proba(datos[corte:])
        t = ya[corte:]
        return float(-np.mean(np.log(
            np.clip(pp[np.arange(len(t)), t], 1e-9, 1))))

    base = perdida(Xa)
    prod = perdida(np.hstack([Xa, (elo * fo).reshape(-1, 1)]))
    asim = perdida(np.hstack([Xa, np.column_stack([
        np.maximum(elo, 0) * np.maximum(-fo, 0),
        np.maximum(-elo, 0) * np.maximum(fo, 0)])]))
    print(f"\n  sin nada {base:.4f} · producto {prod:.4f} "
          f"({base - prod:+.4f}) · asimétrica {asim:.4f} ({base - asim:+.4f})")
    print("  La brecha entre reputación y forma no aporta. Ya estaba medido.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
