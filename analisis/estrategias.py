"""
Estrategias de selección de signo y evaluación de columnas.

Una estrategia decide, para cada casilla, qué signo jugar. La diferencia
entre ellas es qué optimizan:

  favorito_mercado   el signo más probable según el mercado
  favorito_publico   el signo que más gente juega
  value              el signo con mejor ratio probabilidad/popularidad,
                     exigiendo una probabilidad mínima

El value sin probabilidad mínima es contraproducente: lleva a columnas con
ratio altísimo y probabilidad de una entre un millón. Medido sobre 991
jornadas reales, acierta el 27% de los signos frente al 53% del favorito.
El value es un desempate, no un criterio por sí solo.
"""

from __future__ import annotations

from .historico import SIGNOS, CLAVE_PROB, Casilla, JornadaHistorica


def favorito_mercado(c: Casilla) -> str:
    return max(SIGNOS, key=lambda s: c.mercado[CLAVE_PROB[s]])


def favorito_publico(c: Casilla) -> str:
    return max(SIGNOS, key=lambda s: c.lae[CLAVE_PROB[s]])


def value(c: Casilla, probabilidad_minima: float = 0.30) -> str:
    """
    Mejor ratio mercado/público entre los signos suficientemente probables.

    Si ningún signo alcanza el mínimo, cae al favorito del mercado en lugar
    de forzar una elección improbable.
    """
    candidatos = [
        s for s in SIGNOS
        if c.mercado[CLAVE_PROB[s]] >= probabilidad_minima
    ]
    if not candidatos:
        return favorito_mercado(c)
    return max(
        candidatos,
        key=lambda s: c.mercado[CLAVE_PROB[s]] / max(c.lae[CLAVE_PROB[s]], 1e-6),
    )


def columna(jornada: JornadaHistorica, estrategia, **kwargs) -> list[str]:
    """Los 14 signos que jugaría esta estrategia en esta jornada."""
    return [estrategia(c, **kwargs) for c in jornada.casillas_jugables]


def aciertos(columna_jugada: list[str], resultado: str) -> int:
    return sum(1 for i, s in enumerate(columna_jugada) if s == resultado[i])


def probabilidad_columna(
    jornada: JornadaHistorica,
    columna_jugada: list[str],
    fuente: str = "mercado",
) -> float:
    """
    Probabilidad conjunta de que salga exactamente esta columna.

    Con 'mercado' es la probabilidad de que ocurra; con 'lae', la proporción
    de boletos que la contienen, que es lo que determina entre cuántos se
    reparte el premio.
    """
    p = 1.0
    for c, signo in zip(jornada.casillas_jugables, columna_jugada):
        terna = c.mercado if fuente == "mercado" else c.lae
        p *= max(terna[CLAVE_PROB[signo]], 1e-9)
    return p
