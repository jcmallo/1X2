"""
Pleno al 15: la casilla de goles.

La casilla 15 no se juega como 1/X/2 sino como el número de goles de cada
equipo por separado — 0, 1, 2 o M (tres o más) — y hay que acertar los dos.
Marcar varias opciones multiplica las apuestas igual que un doble en el 1X2.

Tiene su propia categoría de premio, la más alta del boleto: mediana de
184.113 EUR sobre 1.090 jornadas.

Cuánto fiarse de esto
---------------------

Menos que del 1X2, y conviene tenerlo presente:

1. El modelo de goles apenas aporta sobre predecir la frecuencia media
   (+0,0109 de log-loss, frente a +0,1651 del Elo de Liga F). Acertar los dos
   marcadores sale al 14,3%. Los goles exactos son casi impredecibles.

2. Existe un sesgo del público real y medido sobre 732 jornadas: infravalora
   que un equipo no marque (0 goles jugado 2,1 veces menos de lo que ocurre)
   y sobrevalora las goleadas (M jugado 1,6 veces de más).

3. Pero el público **se adapta al partido**: en un Getafe-Celta juega 5% al M
   cuando su media histórica es 26,8%. El sesgo no se puede aplicar de forma
   mecánica.

Por eso este módulo exige un valor más alto que el 1X2 para abrir opciones:
si la señal es más débil, el listón sube.
"""

from __future__ import annotations

from dataclasses import dataclass, field


CATEGORIAS = ("0", "1", "2", "M")

# El 1X2 abre a doble con valor > 1,15. Aquí se pide más porque el modelo de
# goles es mucho menos fiable.
VALOR_MINIMO_ABRIR = 1.40
PROBABILIDAD_MINIMA = 0.15


@dataclass
class CasillaPleno:
    local: str
    visitante: str
    p_lae_local: dict[str, float]        # fracciones 0..1
    p_lae_visitante: dict[str, float]
    p_prob_local: dict[str, float] | None = None
    p_prob_visitante: dict[str, float] | None = None

    marcados_local: set[str] = field(default_factory=set)
    marcados_visitante: set[str] = field(default_factory=set)
    motivo: str = ""

    @property
    def tiene_probabilidad(self) -> bool:
        return self.p_prob_local is not None and self.p_prob_visitante is not None

    def valor(self, lado: str, categoria: str) -> float | None:
        prob = self.p_prob_local if lado == "local" else self.p_prob_visitante
        lae = self.p_lae_local if lado == "local" else self.p_lae_visitante
        if not prob:
            return None
        return prob[categoria] / max(lae[categoria], 0.005)

    @property
    def combinaciones(self) -> int:
        return max(1, len(self.marcados_local)) * max(1, len(self.marcados_visitante))


def _elegir_lado(
    prob: dict[str, float] | None,
    lae: dict[str, float],
    aperturas: int,
) -> tuple[set[str], str]:
    """
    Qué categorías marcar en un lado del Pleno.

    Sin probabilidad propia se sigue al público: discrepar sin información
    no es criterio. Con ella, se ordena por valor entre las categorías
    suficientemente probables y se abre solo si el valor supera el listón.
    """
    if not prob:
        return {max(lae, key=lae.get)}, "sin modelo: se sigue al público"

    plausibles = [c for c in CATEGORIAS if prob[c] >= PROBABILIDAD_MINIMA]
    if not plausibles:
        plausibles = [max(prob, key=prob.get)]

    por_valor = sorted(
        plausibles,
        key=lambda c: prob[c] / max(lae[c], 0.005),
        reverse=True,
    )

    elegidas = {por_valor[0]}
    v0 = prob[por_valor[0]] / max(lae[por_valor[0]], 0.005)

    for c in por_valor[1:]:
        if len(elegidas) >= 1 + aperturas:
            break
        v = prob[c] / max(lae[c], 0.005)
        if v >= VALOR_MINIMO_ABRIR:
            elegidas.add(c)

    principal = por_valor[0]
    mas_jugada = max(lae, key=lae.get)
    if principal != mas_jugada:
        motivo = (
            f"'{principal}' en vez de '{mas_jugada}': el público juega "
            f"{lae[mas_jugada]:.0%} a '{mas_jugada}' y el modelo le da "
            f"{prob[mas_jugada]:.0%} (valor {v0:.2f})"
        )
    else:
        motivo = f"'{principal}', coincide con el público (valor {v0:.2f})"

    return elegidas, motivo


def resolver_pleno(casilla: CasillaPleno, aperturas: int = 0) -> CasillaPleno:
    """
    Decide el Pleno. `aperturas` es cuántas categorías extra se permiten por
    lado; cada una multiplica el coste del boleto entero.
    """
    casilla.marcados_local, m1 = _elegir_lado(
        casilla.p_prob_local, casilla.p_lae_local, aperturas
    )
    casilla.marcados_visitante, m2 = _elegir_lado(
        casilla.p_prob_visitante, casilla.p_lae_visitante, aperturas
    )
    casilla.motivo = f"local: {m1} | visitante: {m2}"
    return casilla


def probabilidad_acierto(casilla: CasillaPleno) -> float | None:
    """
    Probabilidad de que alguna de las combinaciones marcadas sea la correcta.

    Asume independencia entre los goles de ambos equipos, que es la
    simplificación del modelo de goles (ver modelado/goles.py).
    """
    if not casilla.tiene_probabilidad:
        return None
    pl = sum(casilla.p_prob_local[c] for c in casilla.marcados_local)
    pv = sum(casilla.p_prob_visitante[c] for c in casilla.marcados_visitante)
    return pl * pv


def resumen(casilla: CasillaPleno) -> str:
    ml = "".join(c for c in CATEGORIAS if c in casilla.marcados_local)
    mv = "".join(c for c in CATEGORIAS if c in casilla.marcados_visitante)
    p = probabilidad_acierto(casilla)
    prob = f"  ·  probabilidad de acertarlo: {p:.1%}" if p is not None else ""
    return (
        f"15. {casilla.local} - {casilla.visitante}\n"
        f"    local {ml}   visitante {mv}   "
        f"({casilla.combinaciones} combinación{'es' if casilla.combinaciones > 1 else ''}){prob}\n"
        f"    {casilla.motivo}"
    )
