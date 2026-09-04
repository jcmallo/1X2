"""
Simulación de carteras de columnas (dobles y triples).

En La Quiniela no se juega una columna: se marcan varios signos en algunos
partidos, y el boleto cubre todas las combinaciones. Cuatro dobles y un
triple son 2^4 x 3 = 48 columnas, y se pagan las 48.

Esto importa porque una sola columna alcanza premio el 13% de las jornadas
y los premios están tan concentrados que el ROI medido con una columna es
ruido (ver HALLAZGOS.md). Diversificar reduce la varianza hasta hacer el
resultado medible.

## Cómo se cuentan los aciertos sin enumerar

Una cartera de 3^14 columnas no se puede recorrer. No hace falta: el número
de columnas con exactamente k aciertos se obtiene multiplicando polinomios,
uno por partido.

Para cada partido con un conjunto S de signos jugados y resultado r:

    si r está en S:   (|S| - 1) + x     una combinación acierta, |S|-1 fallan
    si r no está:     |S|               ninguna acierta

El coeficiente de x^k del producto es cuántas columnas logran k aciertos.
Exacto y en tiempo lineal.
"""

from __future__ import annotations

from .historico import CLAVE_PROB, SIGNOS, JornadaHistorica


def distribucion_aciertos(jugados: list[set[str]], resultado: str) -> list[int]:
    """
    Cuántas columnas de la cartera logran cada número de aciertos.

    Devuelve una lista donde el índice es el número de aciertos.
    """
    dist = [1]
    for i, S in enumerate(jugados):
        n = len(S)
        acierta = resultado[i] in S
        nueva = [0] * (len(dist) + 1)
        for k, c in enumerate(dist):
            if acierta:
                nueva[k] += c * (n - 1)   # se marca un signo que falla
                nueva[k + 1] += c         # se marca el que acierta
            else:
                nueva[k] += c * n         # ninguno acierta
        dist = nueva
    return dist


def total_columnas(jugados: list[set[str]]) -> int:
    t = 1
    for S in jugados:
        t *= len(S)
    return t


# ---------------------------------------------------------------------------
# Criterios para repartir los dobles y triples
# ---------------------------------------------------------------------------

def _entropia(c) -> float:
    """Incertidumbre del partido según el mercado. Alta = más igualado."""
    import math
    h = 0.0
    for s in SIGNOS:
        p = max(c.mercado[CLAVE_PROB[s]], 1e-9)
        h -= p * math.log(p, 3)
    return h


def _value_segundo(c) -> float:
    """
    Cuánto value tiene el segundo signo más probable.

    Es el criterio natural para decidir dónde poner un doble: donde la
    alternativa al favorito está infrajugada por el público.
    """
    orden = sorted(SIGNOS, key=lambda s: c.mercado[CLAVE_PROB[s]], reverse=True)
    segundo = orden[1]
    return c.mercado[CLAVE_PROB[segundo]] / max(c.lae[CLAVE_PROB[segundo]], 1e-6)


def construir(
    jornada: JornadaHistorica,
    n_dobles: int,
    n_triples: int = 0,
    criterio: str = "value",
    probabilidad_minima: float = 0.0,
) -> list[set[str]]:
    """
    Decide qué signos se marcan en cada partido.

    criterio:
      'value'     dobles donde el segundo signo está más infrajugado
      'entropia'  dobles en los partidos más igualados
      'orden'     dobles en los primeros partidos (control, sin criterio)

    En los partidos sin doble ni triple se juega el signo de mejor value
    entre los que superan probabilidad_minima, que es la estrategia que
    mejor resultó en el backtest de columna única.
    """
    casillas = jornada.casillas_jugables

    if criterio == "value":
        puntuar = _value_segundo
    elif criterio == "entropia":
        puntuar = _entropia
    elif criterio == "orden":
        puntuar = lambda c: -c.posicion
    else:
        raise ValueError(f"criterio desconocido: {criterio}")

    ranking = sorted(range(len(casillas)), key=lambda i: puntuar(casillas[i]), reverse=True)
    con_triple = set(ranking[:n_triples])
    con_doble = set(ranking[n_triples:n_triples + n_dobles])

    jugados = []
    for i, c in enumerate(casillas):
        orden = sorted(SIGNOS, key=lambda s: c.mercado[CLAVE_PROB[s]], reverse=True)

        if i in con_triple:
            jugados.append(set(SIGNOS))
        elif i in con_doble:
            jugados.append({orden[0], orden[1]})
        else:
            candidatos = [
                s for s in SIGNOS
                if c.mercado[CLAVE_PROB[s]] >= probabilidad_minima
            ] or [orden[0]]
            elegido = max(
                candidatos,
                key=lambda s: c.mercado[CLAVE_PROB[s]] / max(c.lae[CLAVE_PROB[s]], 1e-6),
            )
            jugados.append({elegido})

    return jugados


# ---------------------------------------------------------------------------
# Evaluación
# ---------------------------------------------------------------------------

def evaluar_cartera(
    jornadas: list[JornadaHistorica],
    n_dobles: int,
    n_triples: int = 0,
    criterio: str = "value",
    probabilidad_minima: float = 0.30,
) -> dict:
    """Aplica la misma configuración de cartera a todas las jornadas."""
    detalle = []
    por_categoria = dict.fromkeys(range(10, 15), 0)

    for j in jornadas:
        jugados = construir(
            j, n_dobles, n_triples, criterio, probabilidad_minima
        )
        n_col = total_columnas(jugados)
        dist = distribucion_aciertos(jugados, j.resultado)

        premio = 0.0
        for k in range(10, min(15, len(dist))):
            if dist[k]:
                premio += dist[k] * j.premios.get(k, 0.0)
                por_categoria[k] += dist[k]

        detalle.append({
            "clave": j.clave,
            "columnas": n_col,
            "coste": n_col * j.precio,
            "premio": premio,
            "mejor": max((k for k, v in enumerate(dist) if v), default=0),
        })

    coste = sum(d["coste"] for d in detalle)
    ganado = sum(d["premio"] for d in detalle)

    return {
        "jornadas": len(detalle),
        "columnas_por_jornada": detalle[0]["columnas"] if detalle else 0,
        "coste": coste,
        "ganado": ganado,
        "roi": (ganado - coste) / coste if coste else 0.0,
        "jornadas_con_premio": sum(1 for d in detalle if d["premio"] > 0),
        "por_categoria": por_categoria,
        "detalle": detalle,
    }
