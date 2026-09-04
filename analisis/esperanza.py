"""
Esperanza matemática de una cartera, con el premio estimado correctamente.

El error que este módulo evita
------------------------------

La versión ingenua del EV multiplica P(acertar k) por el premio medio de la
categoría k. Da resultados absurdos (+4886% para una sola columna) porque
asume que acertar y cobrar son independientes. No lo son.

Medido sobre 989 jornadas: jugando el favorito del mercado se cobra el 3%
de lo que paga esa categoría en una jornada típica; con value, el 9%. La
razón es que se acierta justo cuando sale lo previsible, y entonces han
acertado miles de personas más.

    popularidad de la columna     premio medio cobrado
    Q1 (la menos jugada)                  5,00 EUR
    Q4 (la más jugada)                    1,10 EUR

El premio no es un dato de la categoría: depende de cuánta gente jugó lo
mismo. Por eso aquí se estima así:

    acertantes_k = apuestas_validadas x P_LAE(acertar exactamente k)
    premio_k     = fondo_k / (acertantes_k + columnas_nuestras_k)

donde P_LAE se calcula con la proporción apostada por el público, no con la
probabilidad de mercado. Es el mismo producto de polinomios que en
carteras.py, cambiando qué probabilidad entra.
"""

from __future__ import annotations

from .historico import CLAVE_PROB, JornadaHistorica
from .carteras import construir, total_columnas


def _distribucion(
    jornada: JornadaHistorica,
    jugados: list[set[str]],
    fuente: str,
) -> list[float]:
    """
    P(el resultado caiga dentro de lo marcado en exactamente k partidos).

    Con fuente='mercado' es la probabilidad de que ocurra.
    Con fuente='lae' es la proporción de boletos del público que coinciden
    con nuestra selección en k partidos, que es lo que fija el reparto.
    """
    dist = [1.0]
    for c, marcados in zip(jornada.casillas_jugables, jugados):
        terna = c.mercado if fuente == "mercado" else c.lae
        p = min(max(sum(terna[CLAVE_PROB[s]] for s in marcados), 0.0), 1.0)
        nueva = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nueva[k] += v * (1 - p)
            nueva[k + 1] += v * p
        dist = nueva
    return dist


def _columnas_con_k_aciertos(jugados: list[set[str]], k: int) -> int:
    """
    Cuántas columnas de la cartera logran k aciertos si el resultado cae
    dentro de lo marcado en k partidos.

    En cada partido acertado solo una marca es la buena; en los fallados,
    todas las columnas fallan por igual. Se usa el tamaño medio geométrico
    porque no se sabe qué partidos concretos fallan: exacto para carteras
    homogéneas, aproximado para mixtas.
    """
    total = total_columnas(jugados)
    n = len(jugados)
    fallos = n - k
    if fallos <= 0:
        return 1
    tam_medio = total ** (1.0 / n)
    return max(1, round(tam_medio ** fallos))


def ev_jornada(
    jornada: JornadaHistorica,
    acertantes_reales: dict[int, float],
    jugados: list[set[str]],
) -> dict:
    """Esperanza de premio y coste de una cartera en una jornada."""
    p_mercado = _distribucion(jornada, jugados, "mercado")
    p_lae = _distribucion(jornada, jugados, "lae")
    n_col = total_columnas(jugados)

    ev = 0.0
    desglose = {}

    for k in range(10, min(15, len(p_mercado))):
        prob = p_mercado[k]
        if prob <= 0:
            continue

        premio_unitario = jornada.premios.get(k, 0.0)
        reales = acertantes_reales.get(k, 0.0)
        if premio_unitario <= 0 or reales <= 0:
            # Sin acertantes el fondo se arrastra al siguiente sorteo y no
            # se puede repartir aquí; no se contabiliza.
            continue

        fondo = reales * premio_unitario

        # Cuántos boletos del público comparten nuestro patrón en k partidos.
        # Es la corrección que evita tratar el premio como independiente.
        acertantes_estimados = (
            (jornada.apuestas or 0) * p_lae[k] if k < len(p_lae) else 0.0
        )
        nuestras = _columnas_con_k_aciertos(jugados, k)

        competidores = max(acertantes_estimados, 0.0) + nuestras
        premio = fondo / competidores if competidores > 0 else 0.0

        aporte = prob * premio * nuestras
        ev += aporte
        desglose[k] = {
            "p_mercado": prob,
            "acertantes_estimados": acertantes_estimados,
            "acertantes_reales": reales,
            "premio_estimado": premio,
            "premio_historico": premio_unitario,
            "aporte": aporte,
        }

    return {
        "columnas": n_col,
        "coste": n_col * jornada.precio,
        "ev": ev,
        "desglose": desglose,
    }


def validar_estimacion_acertantes(
    jornadas: list[JornadaHistorica],
    acertantes: dict[str, dict[int, float]],
) -> dict:
    """
    Contrasta los acertantes estimados con los reales del escrutinio.

    Es la prueba de que el modelo de reparto se sostiene: si predice mal
    cuánta gente acierta, el EV que salga de él no vale nada.
    """
    import statistics as st

    comparaciones = {k: [] for k in range(10, 15)}

    for j in jornadas:
        ar = acertantes.get(j.clave_origen)
        if not ar or not j.apuestas:
            continue
        # Cartera trivial: la columna del resultado real. Así se compara
        # manzana con manzana, sin que la estrategia enturbie la medida.
        jugados = [{s} for s in j.resultado]
        p_lae = _distribucion(j, jugados, "lae")

        for k in range(10, 15):
            real = ar.get(k, 0.0)
            if real <= 0 or k >= len(p_lae):
                continue
            est = j.apuestas * p_lae[k]
            if est > 0:
                comparaciones[k].append(est / real)

    return {
        k: {
            "n": len(v),
            "ratio_mediano": st.median(v) if v else None,
        }
        for k, v in comparaciones.items() if v
    }


def evaluar(
    jornadas: list[JornadaHistorica],
    acertantes: dict[str, dict[int, float]],
    n_dobles: int,
    n_triples: int = 0,
    criterio: str = "value",
    probabilidad_minima: float = 0.30,
) -> dict:
    """EV agregada de una configuración de cartera sobre todas las jornadas."""
    coste = ev = 0.0
    n = 0
    for j in jornadas:
        ar = acertantes.get(j.clave_origen)
        if not ar or not j.apuestas:
            continue
        jugados = construir(j, n_dobles, n_triples, criterio, probabilidad_minima)
        r = ev_jornada(j, ar, jugados)
        coste += r["coste"]
        ev += r["ev"]
        n += 1

    return {
        "jornadas": n,
        "columnas_por_jornada": total_columnas(
            construir(jornadas[0], n_dobles, n_triples, criterio, probabilidad_minima)
        ) if jornadas else 0,
        "coste": coste,
        "ev": ev,
        "roi_esperado": (ev - coste) / coste if coste else 0.0,
    }
