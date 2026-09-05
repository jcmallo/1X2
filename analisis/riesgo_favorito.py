"""
¿Sirve de algo la brecha entre reputación y forma?

LA IDEA

    brecha = diferencia de Elo − diferencia de forma

Cuando sale grande y positiva significa "el historial dice que el local es
muy superior, pero los resultados recientes no lo dicen". Villarreal-Depor
en la jornada 4 de 2026-27 es el caso de manual: el Villarreal con 2 puntos
de 9 y el Depor invicto, y el modelo dando un 61% al local.

POR QUÉ PUEDE APORTAR ALGO

La regresión logística es lineal: tiene el Elo y tiene la forma, y las suma.
Puede decir "más Elo, más probable" y "peor forma, menos probable". Lo que no
puede decir de ninguna manera es "un equipo fuerte en mala forma es
DESPROPORCIONADAMENTE vulnerable", porque eso es un producto y una suma nunca
produce un producto.

O sea que no es información que estemos contando mal: es información que el
modelo, tal como está construido, no puede representar.

DOS PREGUNTAS DISTINTAS

    A. ¿Predice mejor el RESULTADO?
       Objeción seria: el mercado ya sabe que el Villarreal lleva 2 puntos,
       y eso ya está en su precio. Si usamos el mercado como entrada,
       añadir esto puede ser contar lo mismo dos veces.

    B. ¿Predice el ERROR DEL PÚBLICO?
       Aquí la objeción no aplica. No decimos que el mercado se equivoque:
       decimos que el público va por detrás del mercado actualizando
       reputaciones. Y como el valor es mercado ÷ público, predecir el error
       del público es predecir el valor, que es lo que se cobra.

    B es la interesante, y es la que nunca hemos probado.

ESTE SCRIPT NO CAMBIA NADA. Solo mide y escribe. Si las correlaciones no
están, la idea se cae y no hemos tocado el modelo.
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
    """
    Identificador de un partido que aguanta pequeñas diferencias de nombre.

    Los resultados y el boleto de la quiniela no siempre escriben igual a los
    equipos ('Ath.Club' contra 'Athletic Club'), así que se compara por los
    primeros caracteres del nombre normalizado más el día. No es perfecto,
    pero el script informa de cuántos ha podido emparejar: si el número sale
    bajo, se ve enseguida en lugar de dar un resultado silenciosamente malo.
    """
    def limpio(s: str) -> str:
        s = (s or "").lower()
        for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"),
                     ("ú", "u"), ("ñ", "n"), (".", ""), (" ", ""), ("-", "")):
            s = s.replace(a, b)
        return s[:6]

    return f"{(fecha or '')[:10]}|{limpio(local)}|{limpio(visitante)}"


def brechas(filas: list[dict]) -> dict[str, float]:
    """
    La brecha de cada partido, calculada con lo que se sabía ANTES de jugarlo.

    Se recorre en orden cronológico llamando a caracteristicas() antes de
    registrar(), igual que hace el entrenamiento. Hacerlo al revés daría una
    brecha calculada con el resultado ya dentro, que es la manera clásica de
    obtener una correlación preciosa y falsa.
    """
    partidos = []
    for f in filas:
        try:
            partidos.append((
                datetime.fromisoformat(f["fecha_hora_inicio"]),
                f["equipo_local"], f["equipo_visitante"],
                int(f["goles_local"]), int(f["goles_visitante"]),
                f.get("signo"), f.get("competicion", ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    partidos.sort()

    estado = caracteristicas.Estado()
    salida: dict[str, float] = {}
    i_elo = caracteristicas.NOMBRES.index("elo_diferencia")
    i_forma = caracteristicas.NOMBRES.index("forma_diferencia")

    for dt, loc, vis, gl, gv, signo, comp in partidos:
        v = estado.caracteristicas(loc, vis, dt)
        # El Elo va dividido por 100 y la forma es una fracción de 0 a 1, así
        # que se ponen en la misma escala antes de restar. Sin esto la brecha
        # sería el Elo con un pellizco de forma.
        salida[clave(dt.isoformat(), loc, vis)] = float(v[i_elo]) - float(v[i_forma])
        estado.registrar(loc, vis, dt, gl, gv)

    return salida


def terna(d: dict | None, a: str, b: str, c: str) -> list[float] | None:
    if not d:
        return None
    try:
        v = [float(d[a]), float(d[b]), float(d[c])]
    except (KeyError, TypeError, ValueError):
        return None
    t = sum(v)
    return [x / t for x in v] if t > 0 else None


def correlacion(x: list[float], y: list[float]) -> tuple[float, float]:
    """
    Pearson, y el intervalo de confianza al 90% por bootstrap.

    Se da el intervalo y no solo el coeficiente porque con muestras pequeñas
    una correlación de 0,20 puede ser perfectamente compatible con cero, y
    entonces no dice nada. El número solo, sin su incertidumbre, invita a
    creerse lo que no toca.
    """
    xa, ya = np.array(x), np.array(y)
    if len(xa) < 20 or xa.std() == 0 or ya.std() == 0:
        return (float("nan"), float("nan"))
    r = float(np.corrcoef(xa, ya)[0, 1])

    rng = np.random.default_rng(11)
    muestras = []
    for _ in range(2000):
        i = rng.integers(0, len(xa), len(xa))
        if xa[i].std() == 0 or ya[i].std() == 0:
            continue
        muestras.append(np.corrcoef(xa[i], ya[i])[0, 1])
    if not muestras:
        return (r, float("nan"))
    lo, hi = np.percentile(muestras, [5, 95])
    return (r, float(hi - lo) / 2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jornadas", type=int, default=50,
                   help="cuántas jornadas mirar hacia atrás")
    args = p.parse_args()

    api = ApiIngesta()

    print("Descargando resultados...")
    filas = descargar_resultados(api)
    print(f"  {len(filas)} partidos con resultado")

    print("Calculando la brecha de cada partido...")
    b = brechas(filas)
    print(f"  {len(b)} brechas")

    # ------------------------------------------------------------------
    # Inventario: ¿hasta dónde llegan los porcentajes de LAE?
    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("INVENTARIO DE PORCENTAJES DE LAE")
    print("=" * 66)

    jornadas = api.contexto_quiniela(limite=min(args.jornadas, 50))
    print(f"  {len(jornadas)} jornadas en la base")

    muestras = []          # (brecha, desvio_publico, nombre)
    resultados = []        # (brecha, gano_el_favorito)
    con_lae = 0
    sin_emparejar = 0

    for j in jornadas:
        try:
            d = api.contexto_dashboard(
                temporada=j.get("etiqueta_temporada"),
                numero_jornada=int(j.get("numero_jornada")),
            )
        except Exception as exc:                     # noqa: BLE001
            print(f"    jornada {j.get('numero_jornada')}: {exc}")
            continue

        hubo = False
        for c in d.get("casillas", []):
            if int(c.get("posicion", 0)) == 15:
                continue
            pr = c.get("probabilidades") or {}
            lae = terna(pr.get("LAE_CIERRE") or pr.get("LAE_ESTIMADO"),
                        "p1", "px", "p2")
            mer = terna(c.get("mercado"), "prob_mercado_local",
                        "prob_mercado_empate", "prob_mercado_visitante")
            if not lae or not mer:
                continue
            hubo = True

            k = clave(c.get("fecha_hora_inicio") or "",
                      c.get("local") or "", c.get("visitante") or "")
            if k not in b:
                sin_emparejar += 1
                continue

            # El favorito lo decide el MERCADO, no nosotros: es el precio
            # con dinero detrás, y la pregunta es si el público se desvía
            # de él.
            i = int(np.argmax(mer))
            desvio = lae[i] - mer[i]      # >0: el público lo juega de más

            # La brecha está orientada al local. Para el visitante hay que
            # darle la vuelta, o los dos casos se cancelarían entre sí.
            brecha = b[k] if i == 0 else (-b[k] if i == 2 else 0.0)
            if i == 1:
                continue                  # el empate no tiene "reputación"

            muestras.append((brecha, desvio,
                             f"{c.get('local')} - {c.get('visitante')}"))

        if hubo:
            con_lae += 1

    print(f"  {con_lae} jornadas con porcentajes de LAE Y cuotas")
    print(f"  {len(muestras)} casillas utilizables")
    if sin_emparejar:
        print(f"  {sin_emparejar} casillas que no casan con ningún resultado "
              "(nombres distintos o partido aún sin jugar)")

    # ------------------------------------------------------------------
    # B. ¿Predice la brecha el error del público?
    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("B · ¿SOBREJUEGA EL PÚBLICO AL FAVORITO CON REPUTACIÓN?")
    print("=" * 66)

    if len(muestras) < 30:
        print(f"\n  Solo hay {len(muestras)} casillas. Hacen falta bastantes")
        print("  más para distinguir una correlación del ruido.")
        print("\n  No es un fallo: la captura de LAE empezó con este")
        print("  proyecto. Se puede repetir dentro de unos meses, y para")
        print("  entonces cada jornada habrá añadido 14 casillas.")
    else:
        xs = [m[0] for m in muestras]
        ys = [m[1] for m in muestras]
        r, err = correlacion(xs, ys)
        print(f"\n  n = {len(muestras)}")
        print(f"  correlación brecha ↔ (LAE − mercado) del favorito: "
              f"{r:+.3f} ± {err:.3f}")
        print()
        if not np.isnan(err) and abs(r) - err > 0.05:
            print("  Hay señal. Cuanto mayor es la brecha entre reputación y")
            print("  forma, más sobrejuega el público al favorito. Eso es")
            print("  exactamente el valor que busca el sistema.")
        else:
            print("  El intervalo incluye el cero o casi: con estos datos no")
            print("  se puede afirmar que exista relación. No es que no la")
            print("  haya; es que no se ve todavía.")

        # Los casos extremos, para poder mirarlos a mano.
        muestras.sort(key=lambda m: -m[0])
        print("\n  Las cinco de mayor brecha:")
        for br, de, nom in muestras[:5]:
            print(f"    {nom[:44]:<44} brecha {br:>+6.2f}  "
                  f"público {de:>+.1%}")

    # ------------------------------------------------------------------
    # A. ¿Predice la brecha el resultado, más allá de Elo y forma?
    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("A · ¿PREDICE MEJOR EL RESULTADO?")
    print("=" * 66)

    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, meta, _ = caracteristicas.construir(filas)
    Xa, ya = np.array(X), np.array(y)
    i_elo = caracteristicas.NOMBRES.index("elo_diferencia")
    i_forma = caracteristicas.NOMBRES.index("forma_diferencia")
    extra = (Xa[:, i_elo] - Xa[:, i_forma]).reshape(-1, 1)
    Xb = np.hstack([Xa, extra])

    corte = int(len(Xa) * 0.80)
    print(f"\n  {corte} para entrenar, {len(Xa) - corte} para comprobar")
    print("  (corte por fecha, nunca al azar)")

    def perdida(datos):
        m = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=2000, C=0.5))
        m.fit(datos[:corte], ya[:corte])
        p = m.predict_proba(datos[corte:])
        t = ya[corte:]
        return float(-np.mean(np.log(np.clip(p[np.arange(len(t)), t], 1e-9, 1))))

    ll_sin = perdida(Xa)
    ll_con = perdida(Xb)
    print(f"\n  {'sin la brecha':<20} log-loss {ll_sin:.4f}")
    print(f"  {'con la brecha':<20} log-loss {ll_con:.4f}")
    print(f"  {'diferencia':<20}          {ll_sin - ll_con:+.4f}")
    print()
    if ll_sin - ll_con > 0.002:
        print("  Aporta. Y recuerda por qué puede aportar aunque el Elo y la")
        print("  forma ya estén dentro: la regresión los SUMA, y esto es un")
        print("  producto que una suma no puede expresar.")
    else:
        print("  No aporta al resultado. Era lo esperable: el mercado ya")
        print("  descuenta la mala racha del favorito, así que esa")
        print("  información no es nueva para predecir quién gana.")
        print("  Eso no dice nada sobre la pregunta B, que es la que importa.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
