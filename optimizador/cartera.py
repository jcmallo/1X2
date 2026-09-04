"""
Optimizador de cartera para una jornada.

Decide en qué partidos abrir doble o triple y qué signo jugar en el resto,
partiendo de dos probabilidades por casilla:

    p_prob   lo que va a pasar        (mercado, o modelo propio si no hay mercado)
    p_lae    lo que juega la gente    (porcentajes oficiales de LAE)

Criterios que salen del análisis del histórico (ver analisis/HALLAZGOS.md):

1. El premio lo reparte el público, no el mercado. Un signo muy probable pero
   muy jugado paga poco: la columna menos jugada cobra 4,5 veces más que la
   más jugada.

2. El valor solo sirve como desempate. Elegir por ratio sin exigir una
   probabilidad mínima lleva a columnas absurdas: sobre 989 jornadas reales
   acertaba el 27% de los signos frente al 53% del favorito.

3. Ampliar la cartera reduce la varianza pero NO aumenta el valor esperado.
   El fondo de cada categoría es fijo: si tus columnas acaparan una
   categoría, cobras ese fondo repartido entre tus propias columnas. Por eso
   el optimizador no persigue maximizar columnas, sino cubrir las casillas
   donde la incertidumbre es alta o el público está más desviado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math


SIGNOS = ("1", "X", "2")


@dataclass
class Casilla:
    """Una casilla del boleto con lo que sabemos de ella."""
    posicion: int
    local: str
    visitante: str
    p_lae: dict[str, float]            # proporción apostada, fracciones 0..1
    p_prob: dict[str, float] | None = None   # probabilidad, None si no hay
    fuente_prob: str = "sin_datos"     # 'mercado' | 'modelo' | 'sin_datos'

    marcados: set[str] = field(default_factory=set)
    motivo: str = ""

    @property
    def tiene_probabilidad(self) -> bool:
        return self.p_prob is not None

    def valor(self, signo: str) -> float | None:
        """Cuánto vale ese signo: probabilidad partido por popularidad."""
        if not self.p_prob:
            return None
        return self.p_prob[signo] / max(self.p_lae[signo], 0.005)

    def entropia(self) -> float:
        """Incertidumbre en base 3. 1 = totalmente abierto."""
        fuente = self.p_prob or self.p_lae
        h = 0.0
        for s in SIGNOS:
            p = max(fuente[s], 1e-9)
            h -= p * math.log(p, 3)
        return h

    def favorito(self) -> str:
        fuente = self.p_prob or self.p_lae
        return max(SIGNOS, key=lambda s: fuente[s])


def _prioridad_apertura(c: Casilla) -> float:
    """
    Cuánto merece la pena abrir esta casilla a doble.

    Combina dos cosas: cuán incierto está el partido y cuánto se desvía el
    público del mercado. Una casilla donde el segundo signo está infrajugado
    es mejor candidata que una simplemente igualada, porque ahí el doble no
    solo cubre riesgo: además compra valor.

    Sin probabilidad propia solo queda la incertidumbre del público, así que
    esas casillas puntúan por entropía pero penalizadas: abrir a ciegas
    cuesta lo mismo que abrir con criterio y rinde menos.
    """
    h = c.entropia()

    if not c.tiene_probabilidad:
        return h * 0.6

    orden = sorted(SIGNOS, key=lambda s: c.p_prob[s], reverse=True)
    v_segundo = c.valor(orden[1]) or 0.0
    return h * min(v_segundo, 2.5)


def construir_cartera(
    casillas: list[Casilla],
    presupuesto_columnas: int = 16,
    probabilidad_minima: float = 0.30,
) -> dict:
    """
    Reparte dobles hasta agotar el presupuesto de columnas.

    presupuesto_columnas es el tope de combinaciones del boleto (cada doble
    multiplica por 2, cada triple por 3). Con 16 caben 4 dobles; con 64, seis.
    """
    if presupuesto_columnas < 1:
        raise ValueError("El presupuesto debe ser de al menos una columna.")

    orden = sorted(casillas, key=_prioridad_apertura, reverse=True)

    columnas = 1
    abiertas: set[int] = set()
    for c in orden:
        if columnas * 2 > presupuesto_columnas:
            break
        abiertas.add(c.posicion)
        columnas *= 2

    for c in casillas:
        if c.posicion in abiertas:
            c.marcados = _elegir_doble(c, probabilidad_minima)
            c.motivo = (
                "doble: partido abierto, se acompaña al favorito con el signo "
                "de mejor valor"
                if c.tiene_probabilidad
                else "doble: sin datos propios, se cubre la incertidumbre"
            )
        else:
            c.marcados = {_elegir_simple(c, probabilidad_minima)}
            c.motivo = _explicar_simple(c, next(iter(c.marcados)), probabilidad_minima)

    return {
        "columnas": columnas,
        "dobles": len(abiertas),
        "casillas": casillas,
    }


def _elegir_doble(c: Casilla, probabilidad_minima: float) -> set[str]:
    """
    Qué dos signos marcar.

    El favorito entra siempre: es la cobertura. El acompañante se elige por
    valor y no por probabilidad, que es la diferencia que hace útil el doble.

    El caso que lo motiva: Rayo-Racing con mercado 43/29/29 y público
    56/26/18. La X y el 2 empatan en probabilidad, pero el 2 vale 1,59 y la X
    1,10, porque el público apenas juega el 2. Elegir por probabilidad
    dejaría fuera el mejor signo de la jornada por un desempate arbitrario.

    Solo se consideran acompañantes con una probabilidad mínima razonable
    (la mitad del umbral): un signo de valor altísimo pero improbable no
    cubre nada, solo encarece el boleto.
    """
    fuente = c.p_prob or c.p_lae
    favorito = max(SIGNOS, key=lambda s: fuente[s])

    if not c.tiene_probabilidad:
        segundo = max(
            (s for s in SIGNOS if s != favorito),
            key=lambda s: fuente[s],
        )
        return {favorito, segundo}

    minimo = probabilidad_minima / 2
    resto = [s for s in SIGNOS if s != favorito]
    plausibles = [s for s in resto if c.p_prob[s] >= minimo] or resto

    segundo = max(plausibles, key=lambda s: c.valor(s) or 0.0)
    return {favorito, segundo}


def _elegir_simple(c: Casilla, probabilidad_minima: float) -> str:
    """
    Signo único: el de mejor valor entre los suficientemente probables.

    Sin probabilidad propia no se puede calcular valor, así que se sigue al
    público. Es lo menos malo: discrepar sin información no es criterio, es
    ruido.
    """
    if not c.tiene_probabilidad:
        return max(SIGNOS, key=lambda s: c.p_lae[s])

    candidatos = [s for s in SIGNOS if c.p_prob[s] >= probabilidad_minima]
    if not candidatos:
        return c.favorito()
    return max(candidatos, key=lambda s: c.valor(s) or 0.0)


def _explicar_simple(c: Casilla, signo: str, probabilidad_minima: float) -> str:
    if not c.tiene_probabilidad:
        return "sin datos propios: se sigue al público"

    v = c.valor(signo) or 0.0
    fav = c.favorito()

    if signo != fav:
        # El valor es probabilidad / popularidad: por encima de 1 el signo
        # está infrajugado y paga más de lo que le toca; en 1 paga lo normal.
        # Decir "paga mejor" con valor 1,00 sería falso, y este texto es lo
        # que se lee al decidir si jugar el boleto.
        if v >= 1.05:
            reparto = f"este paga mejor (valor {v:.2f})"
        else:
            reparto = (
                f"este no paga mejor (valor {v:.2f}), pero el favorito paga "
                f"peor todavía ({c.valor(fav) or 0.0:.2f})"
            )
        return (
            f"no es el favorito, pero el favorito está sobrejugado "
            f"({c.p_lae[fav]:.0%} lo juega); {reparto}"
        )
    if v >= 1.15:
        return f"favorito e infrajugado (valor {v:.2f})"
    if v < 0.9:
        return f"favorito pero sobrejugado (valor {v:.2f}); no hay alternativa con probabilidad suficiente"
    return f"favorito, valor razonable ({v:.2f})"


def resumen_texto(cartera: dict, precio_columna: float = 0.75) -> str:
    """Boleto en texto, con el porqué de cada casilla."""
    lineas = []
    cas = sorted(cartera["casillas"], key=lambda c: c.posicion)

    ancho = max(len(f"{c.local} - {c.visitante}") for c in cas)
    for c in cas:
        marca = "".join(s for s in SIGNOS if s in c.marcados)
        etq = f"{c.local} - {c.visitante}"
        lineas.append(f"{c.posicion:>2}. {etq:<{ancho}}  {marca:<3}  {c.motivo}")

    coste = cartera["columnas"] * precio_columna
    lineas.append("")
    lineas.append(
        f"{cartera['columnas']} columnas "
        f"({cartera['dobles']} dobles) = {coste:.2f} EUR"
    )
    return "\n".join(lineas)
