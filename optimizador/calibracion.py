"""
Convierte lo que juega el público en una probabilidad, sin usar cuotas.

POR QUÉ EXISTE

El valor de un signo es `probabilidad ÷ popularidad`, y hasta ahora la
probabilidad venía del mercado. El problema es que el mercado no siempre
está: en Liga F apenas hay casas que la coticen, y cuando las hay publican
un precio y no lo tocan en toda la semana —las cuatro capturas de una
jornada salen idénticas—, así que no es un mercado informativo sino un
número puesto ahí. En el histórico directamente no existe: de 1.053
casillas medidas, solo 2 tenían cuotas.

Sin probabilidad no hay valor, y sin valor el optimizador se limita a
seguir al público, que es justo lo contrario de lo que debe hacer.

DE DÓNDE SALE LA TABLA

De medir, sobre 190 jornadas y 3.159 observaciones (los tres signos de
1.053 casillas), cuántas veces ocurre de verdad cada signo según lo que
juega la gente:

    el público dice   ocurre    valor
          6,4%        23,1%      3,62
         14,7%        25,9%      1,76
         24,7%        32,5%      1,32
         33,5%        31,4%      0,94
         44,1%        40,9%      0,93
         54,5%        36,9%      0,68
         64,9%        44,6%      0,69
         79,5%        49,8%      0,63

Un signo que juega el 80% de la gente sale la mitad de las veces. Uno que
juega menos del 10% sale el 23%. Es el sesgo favorito-tapado de libro, y es
enorme: factor 5,7 entre extremos.

Ese número coincide con el 4,5 que ya habíamos medido en los premios reales
del histórico, por una vía completamente distinta —allí euros pagados, aquí
frecuencias observadas—. Que dos medidas independientes den lo mismo es lo
que da confianza en la tabla.

POR QUÉ SE SUAVIZA

La tabla observada no es monótona: el tramo del 33,5% ocurre menos que el
del 24,7%, y el del 54,5% menos que el del 44,1%. Eso es ruido de muestra
—esos tramos tienen entre 200 y 550 casos—, pero usado tal cual haría que
un signo MÁS jugado recibiera MENOS probabilidad, con saltos absurdos en el
boleto.

Se corrige con regresión isotónica ponderada por el número de casos, que es
la herramienta estándar para calibrar: busca la curva creciente más cercana
a lo observado. Solo mueve los cuatro puntos que rompían el orden, y como
mucho dos puntos porcentuales.

LO QUE ESTO NO ES

No es una predicción del partido: no sabe quién juega. Es una afirmación
sobre el comportamiento del público, y por eso funciona igual en Liga F que
en Primera. Cuando haya mercado de verdad, el mercado manda: sabe del
partido y esto no.
"""

from __future__ import annotations

# Puntos de la curva ya suavizada: (lo que juega el público, lo que ocurre).
CURVA: tuple[tuple[float, float], ...] = (
    (0.064, 0.231),
    (0.147, 0.259),
    (0.247, 0.320),
    (0.335, 0.320),
    (0.441, 0.390),
    (0.545, 0.390),
    (0.649, 0.446),
    (0.795, 0.498),
)


def probabilidad(p_publico: float) -> float:
    """
    Lo que ocurre de verdad cuando el público juega ese porcentaje.

    Interpola entre los puntos medidos. Fuera del rango se queda en el
    extremo: extrapolar una curva de ocho puntos más allá de donde hay datos
    es inventarse números.
    """
    if p_publico <= CURVA[0][0]:
        return CURVA[0][1]
    if p_publico >= CURVA[-1][0]:
        return CURVA[-1][1]

    for (x0, y0), (x1, y1) in zip(CURVA, CURVA[1:]):
        if x0 <= p_publico <= x1:
            if x1 == x0:
                return y0
            t = (p_publico - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return CURVA[-1][1]


def desde_publico(p_lae: dict[str, float]) -> dict[str, float] | None:
    """
    La terna de probabilidades que corresponde a lo que juega el público.

    Se normaliza para que sume 1: la curva se aplica a cada signo por
    separado y no tiene por qué cuadrar sola. Sin normalizar, casillas muy
    repartidas y casillas muy concentradas se compararían en escalas
    distintas y el valor dejaría de ser comparable entre ellas.
    """
    if not p_lae:
        return None
    try:
        crudas = {s: probabilidad(float(p_lae[s])) for s in ("1", "X", "2")}
    except (KeyError, TypeError, ValueError):
        return None
    total = sum(crudas.values())
    if total <= 0:
        return None
    return {s: v / total for s, v in crudas.items()}
