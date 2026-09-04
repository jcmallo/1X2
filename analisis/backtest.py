"""
Backtest de estrategias contra premios reales.

Uso:
    python -m analisis.backtest --datos /ruta/a/los/xlsb

Importante sobre la interpretación de los resultados:

El ROI de una columna única NO es estadísticamente concluyente con este
histórico. Una columna alcanza premio (10 o más aciertos) el 13% de las
jornadas, y los premios están tan concentrados que un único acierto de 12
en una jornada favorable puede aportar la mitad de todo lo ganado en 17
años. Medido: con umbral 27% el ROI aparente era +15,8%, pero el 53% de esa
ganancia venía de una sola jornada (2017/18_08); sin ella, -45,4%.

Por eso este módulo acompaña todo ROI de un bootstrap. Si el intervalo de
confianza cruza el cero por mucho, el número no significa nada, por bonito
que sea.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics as st

from .historico import cargar, solo_completas, CLAVE_PROB, SIGNOS
from .estrategias import (
    favorito_mercado, favorito_publico, value,
    columna, aciertos, probabilidad_columna,
)


CATEGORIA_MINIMA = 10  # por debajo de 10 aciertos no hay premio


def evaluar(jornadas, estrategia, **kwargs) -> dict:
    """Aplica una estrategia a todas las jornadas y acumula el resultado."""
    detalle = []
    signos_ok = signos_tot = 0
    por_categoria = dict.fromkeys(range(CATEGORIA_MINIMA, 15), 0)

    for j in jornadas:
        col = columna(j, estrategia, **kwargs)
        ac = aciertos(col, j.resultado)

        signos_ok += ac
        signos_tot += 14

        premio = j.premios.get(ac, 0.0) if ac >= CATEGORIA_MINIMA else 0.0
        if ac >= CATEGORIA_MINIMA:
            por_categoria[ac] += 1

        detalle.append({
            "clave": j.clave,
            "aciertos": ac,
            "coste": j.precio,
            "premio": premio,
            "p_mercado": probabilidad_columna(j, col, "mercado"),
            "p_lae": probabilidad_columna(j, col, "lae"),
        })

    coste = sum(d["coste"] for d in detalle)
    ganado = sum(d["premio"] for d in detalle)

    return {
        "jornadas": len(detalle),
        "acierto_signo": signos_ok / signos_tot if signos_tot else 0.0,
        "coste": coste,
        "ganado": ganado,
        "roi": (ganado - coste) / coste if coste else 0.0,
        "por_categoria": por_categoria,
        "detalle": detalle,
    }


def bootstrap(detalle: list[dict], repeticiones: int = 2000, semilla: int = 42) -> dict:
    """
    Remuestrea jornadas con reemplazo para estimar la incertidumbre del ROI.

    Es la comprobación que separa una estrategia de una casualidad: si el
    intervalo del 90% cruza el cero holgadamente, no hay evidencia de nada.
    """
    random.seed(semilla)
    n = len(detalle)
    rois = []

    for _ in range(repeticiones):
        muestra = [detalle[random.randrange(n)] for _ in range(n)]
        c = sum(d["coste"] for d in muestra)
        g = sum(d["premio"] for d in muestra)
        rois.append((g - c) / c if c else 0.0)

    rois.sort()
    return {
        "mediana": rois[len(rois) // 2],
        "p05": rois[int(0.05 * len(rois))],
        "p95": rois[int(0.95 * len(rois))],
        "prob_positivo": sum(1 for r in rois if r > 0) / len(rois),
    }


def concentracion(detalle: list[dict]) -> dict:
    """Cuánto del total ganado depende del premio más grande."""
    premios = sorted((d["premio"] for d in detalle if d["premio"] > 0), reverse=True)
    total = sum(premios)
    if not premios or total == 0:
        return {"n_premios": 0, "peso_mayor": 0.0, "peso_top5": 0.0}
    return {
        "n_premios": len(premios),
        "peso_mayor": premios[0] / total,
        "peso_top5": sum(premios[:5]) / total,
    }


def calibracion(jornadas) -> dict:
    """
    Compara mercado y público como estimadores de la probabilidad real.

    Sobre 14.139 casillas el mercado acierta el 53,02% de los signos y el
    público el 52,13%, con log-loss 0,967 frente a 0,999. El mercado está
    calibrado a ~1 punto porcentual en todos los tramos; el público
    sobreapuesta los favoritos moderados hasta 10 puntos.
    """
    import math

    n = ok_m = ok_l = 0
    ll_m = ll_l = 0.0
    tramos = {}

    for j in jornadas:
        for c in j.casillas_jugables:
            if not c.signo:
                continue
            k = CLAVE_PROB[c.signo]
            n += 1
            ok_m += max(SIGNOS, key=lambda s: c.mercado[CLAVE_PROB[s]]) == c.signo
            ok_l += max(SIGNOS, key=lambda s: c.lae[CLAVE_PROB[s]]) == c.signo
            ll_m -= math.log(max(c.mercado[k], 1e-9))
            ll_l -= math.log(max(c.lae[k], 1e-9))

            tramo = min(int(c.mercado["p1"] * 10), 9)
            t = tramos.setdefault(tramo, {"n": 0, "mkt": 0.0, "lae": 0.0, "real": 0})
            t["n"] += 1
            t["mkt"] += c.mercado["p1"]
            t["lae"] += c.lae["p1"]
            t["real"] += (c.signo == "1")

    return {
        "casillas": n,
        "acierto_mercado": ok_m / n if n else 0,
        "acierto_publico": ok_l / n if n else 0,
        "logloss_mercado": ll_m / n if n else 0,
        "logloss_publico": ll_l / n if n else 0,
        "tramos": {
            k: {
                "n": v["n"],
                "mercado": v["mkt"] / v["n"],
                "publico": v["lae"] / v["n"],
                "real": v["real"] / v["n"],
            }
            for k, v in sorted(tramos.items()) if v["n"] >= 50
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Backtest sobre el histórico real.")
    p.add_argument(
        "--datos", required=True,
        help="carpeta con los dos .xlsb de Quinielandia",
    )
    p.add_argument("--bootstrap", type=int, default=2000)
    args = p.parse_args()

    val = os.path.join(args.datos, "ValoracionesCoeficiente_BtF_LAE.xlsb")
    est = os.path.join(args.datos, "Estadistica Real.xlsb")
    for f in (val, est):
        if not os.path.isfile(f):
            print(f"No encuentro: {f}")
            return 1

    print("Cargando histórico...")
    todas = cargar(val, est)
    jornadas = solo_completas(todas)
    print(f"  jornadas leídas: {len(todas)}")
    print(f"  con resultado, precio y 14 casillas: {len(jornadas)}")

    cal = calibracion(jornadas)
    print()
    print(f"=== Calibración ({cal['casillas']:,} casillas) ===")
    print(f"  acierto  mercado {cal['acierto_mercado']:.2%}   público {cal['acierto_publico']:.2%}")
    print(f"  log-loss mercado {cal['logloss_mercado']:.4f}   público {cal['logloss_publico']:.4f}")
    print()
    print(f"  {'tramo':>10} {'n':>6} {'mercado':>9} {'público':>9} {'real':>8} {'sesgo':>8}")
    for k, t in cal["tramos"].items():
        print(f"  {k*10:>3}-{k*10+10:<6} {t['n']:>6} {t['mercado']:>9.3f} "
              f"{t['publico']:>9.3f} {t['real']:>8.3f} {t['publico']-t['real']:>+8.3f}")

    pruebas = [("favorito mercado", favorito_mercado, {}),
               ("favorito público", favorito_publico, {})]
    pruebas += [(f"value min {u:.0%}", value, {"probabilidad_minima": u})
                for u in (0.25, 0.30, 0.35)]

    print()
    print("=== Backtest con premios reales, una columna por jornada ===")
    print(f"  {'estrategia':<20} {'acc':>7} {'coste':>8} {'ganado':>9} {'ROI':>8} "
          f"{'ROI mediana':>12} {'IC90':>18} {'P(>0)':>7}")
    print("  " + "-" * 96)

    for nombre, fn, kw in pruebas:
        r = evaluar(jornadas, fn, **kw)
        b = bootstrap(r["detalle"], args.bootstrap)
        ic = f"[{b['p05']:+.0%}, {b['p95']:+.0%}]"
        print(f"  {nombre:<20} {r['acierto_signo']:>6.1%} {r['coste']:>8,.0f} "
              f"{r['ganado']:>9,.0f} {r['roi']:>+7.1%} {b['mediana']:>+11.1%} "
              f"{ic:>18} {b['prob_positivo']:>6.0%}")

    print()
    print("=== Concentración del premio (por qué el ROI no es concluyente) ===")
    r = evaluar(jornadas, value, probabilidad_minima=0.27)
    c = concentracion(r["detalle"])
    print(f"  jornadas con premio: {c['n_premios']} de {r['jornadas']}")
    print(f"  el premio mayor aporta el {c['peso_mayor']:.0%} de todo lo ganado")
    print(f"  los cinco mayores aportan el {c['peso_top5']:.0%}")
    print()
    print("  Con esta concentración, el ROI de una columna única mide suerte,")
    print("  no estrategia. Hacen falta carteras de muchas columnas.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
