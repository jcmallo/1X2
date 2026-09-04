"""
Modelo Elo con empate, para competiciones sin mercado de apuestas.

Por qué existe
--------------

El análisis del histórico mostró que la probabilidad de mercado está
calibrada a ~1 punto porcentual (ver analisis/HALLAZGOS.md). Intentar
superarla en Primera y Segunda es pelear contra un rival muy fuerte con poco
que ganar.

Liga F es otra cosa: no hay mercado. The Odds API no la cubre y las casas
apenas dan cuotas. Y la Liga F ocupa 4 de las 15 casillas del boleto. Ahí un
modelo propio no compite con nadie: es la única fuente de probabilidad
disponible.

Cómo trata el empate
--------------------

El Elo clásico da un único número: la expectativa de puntuación del local,
que mezcla victoria y empate. Para repartirlo en 1/X/2 hace falta saber cuán
probable es el empate, y eso depende de lo igualado que esté el partido: dos
equipos parejos empatan más que un favorito claro contra un colista.

Aquí la probabilidad de empate se calibra con los propios datos, agrupando
los partidos por diferencia de Elo y midiendo qué fracción acabó en empate.
Nada de constantes copiadas de otro sitio.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field


ELO_INICIAL = 1500.0


@dataclass
class Partido:
    fecha: str
    local: str
    visitante: str
    goles_local: int
    goles_visitante: int
    signo: str
    competicion: str = ""

    @property
    def resultado_local(self) -> float:
        """1 si gana el local, 0,5 si empate, 0 si gana el visitante."""
        if self.goles_local > self.goles_visitante:
            return 1.0
        if self.goles_local == self.goles_visitante:
            return 0.5
        return 0.0


@dataclass
class ModeloElo:
    """
    k          cuánto mueve cada partido la valoración
    ventaja    puntos de Elo que vale jugar en casa
    tramos     probabilidad de empate por tramo de diferencia de Elo
    """
    k: float = 20.0
    ventaja_local: float = 60.0
    rating: dict[str, float] = field(default_factory=dict)
    tramos_empate: dict[int, float] = field(default_factory=dict)
    prob_empate_global: float = 0.25

    def valoracion(self, equipo: str) -> float:
        return self.rating.get(equipo, ELO_INICIAL)

    def esperanza_local(self, local: str, visitante: str) -> float:
        """Expectativa Elo del local, entre 0 y 1. Incluye media victoria por empate."""
        diff = self.valoracion(local) + self.ventaja_local - self.valoracion(visitante)
        return 1.0 / (1.0 + 10 ** (-diff / 400.0))

    def _tramo(self, diff: float) -> int:
        """Agrupa la diferencia de Elo en escalones de 50 puntos, tope 400."""
        return min(int(abs(diff) // 50), 8)

    def probabilidad_empate(self, local: str, visitante: str) -> float:
        diff = self.valoracion(local) + self.ventaja_local - self.valoracion(visitante)
        return self.tramos_empate.get(self._tramo(diff), self.prob_empate_global)

    def predecir(self, local: str, visitante: str) -> dict[str, float]:
        """
        Probabilidades 1/X/2.

        La expectativa Elo E vale P(1) + P(X)/2. Conocida P(X) por
        calibración, se despeja P(1) y el resto es P(2). Se recortan a un
        mínimo por si un caso extremo empuja alguna por debajo de cero.
        """
        e = self.esperanza_local(local, visitante)
        px = self.probabilidad_empate(local, visitante)

        p1 = e - px / 2.0
        p2 = 1.0 - px - p1

        p1 = max(p1, 0.01)
        p2 = max(p2, 0.01)
        total = p1 + px + p2
        return {"1": p1 / total, "X": px / total, "2": p2 / total}

    # -----------------------------------------------------------------
    # Entrenamiento
    # -----------------------------------------------------------------

    def entrenar(self, partidos: list[Partido]) -> None:
        """
        Recorre los partidos en orden cronológico actualizando valoraciones.

        El orden importa: procesar un partido de 2023 después de uno de 2026
        contaminaría la valoración con información del futuro.
        """
        ordenados = sorted(partidos, key=lambda p: p.fecha)

        for p in ordenados:
            rl = self.valoracion(p.local)
            rv = self.valoracion(p.visitante)
            esperado = 1.0 / (1.0 + 10 ** (-(rl + self.ventaja_local - rv) / 400.0))
            ajuste = self.k * (p.resultado_local - esperado)
            self.rating[p.local] = rl + ajuste
            self.rating[p.visitante] = rv - ajuste

        self._calibrar_empates(ordenados)

    def _calibrar_empates(self, partidos: list[Partido]) -> None:
        """
        Mide la frecuencia real de empate por tramo de diferencia de Elo.

        Se hace en una segunda pasada, con las valoraciones ya asentadas.
        Los tramos con pocos partidos se dejan en la media global: estimar
        una probabilidad con veinte casos es inventarla.
        """
        conteo: dict[int, list[int]] = defaultdict(list)
        empates = 0

        for p in partidos:
            diff = self.valoracion(p.local) + self.ventaja_local - self.valoracion(p.visitante)
            es_empate = 1 if p.signo == "X" else 0
            conteo[self._tramo(diff)].append(es_empate)
            empates += es_empate

        self.prob_empate_global = empates / len(partidos) if partidos else 0.25

        self.tramos_empate = {
            tramo: sum(v) / len(v)
            for tramo, v in conteo.items()
            if len(v) >= 30
        }


# ---------------------------------------------------------------------------
# Evaluación
# ---------------------------------------------------------------------------

def evaluar(modelo: ModeloElo, partidos: list[Partido]) -> dict:
    """
    Mide el modelo sobre partidos que no ha visto.

    Se informa acierto y log-loss, y como referencia el log-loss de predecir
    siempre la frecuencia base. Si el modelo no bate a esa referencia, no
    está aportando nada.
    """
    if not partidos:
        return {}

    aciertos = 0
    ll = 0.0
    frec = {"1": 0, "X": 0, "2": 0}

    for p in partidos:
        frec[p.signo] += 1

    base = {s: max(frec[s] / len(partidos), 1e-9) for s in frec}
    ll_base = 0.0

    for p in partidos:
        pr = modelo.predecir(p.local, p.visitante)
        if max(pr, key=pr.get) == p.signo:
            aciertos += 1
        ll -= math.log(max(pr[p.signo], 1e-9))
        ll_base -= math.log(base[p.signo])

    n = len(partidos)
    return {
        "n": n,
        "acierto": aciertos / n,
        "logloss": ll / n,
        "logloss_base": ll_base / n,
        "mejora_sobre_base": (ll_base - ll) / n,
        "frecuencias": {s: frec[s] / n for s in frec},
    }


def dividir_temporal(partidos: list[Partido], fraccion_train: float = 0.75):
    """
    Separa entrenamiento y prueba por fecha, nunca al azar.

    Un split aleatorio dejaría partidos de 2026 en entrenamiento y de 2023 en
    prueba: el modelo conocería el futuro y el resultado sería optimista y
    falso.
    """
    ordenados = sorted(partidos, key=lambda p: p.fecha)
    corte = int(len(ordenados) * fraccion_train)
    return ordenados[:corte], ordenados[corte:]
