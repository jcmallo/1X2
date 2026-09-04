"""
Modelo de goles para el Pleno al 15.

Qué se pide en esa casilla
--------------------------

El Pleno al 15 no se juega como 1/X/2 sino como el número de goles de cada
equipo por separado: 0, 1, 2 o M (tres o más). Hay que acertar los dos, así
que son 16 combinaciones posibles.

Tiene su propia categoría de premio, la más alta del boleto: mediana de
184.113 EUR sobre 1.090 jornadas, en línea con la categoría de 14.

Cómo se modela
--------------

Poisson por equipo, con fuerzas de ataque y defensa estimadas del histórico:

    lambda_local     = media_liga x ataque(local)  x defensa(visitante) x ventaja
    lambda_visitante = media_liga x ataque(visitante) x defensa(local)

De ahí salen P(0), P(1), P(2) y P(3 o más) para cada equipo.

Limitación conocida: Poisson trata los goles de los dos equipos como
independientes, y no lo son del todo (un equipo que va perdiendo ataca más).
El efecto es conocido y afecta sobre todo a los marcadores bajos; corregirlo
requiere un ajuste tipo Dixon-Coles que aquí no se aplica. Se deja anotado
en lugar de fingir que el modelo es exacto.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .elo import Partido


CATEGORIAS = ("0", "1", "2", "M")


@dataclass
class ModeloGoles:
    """
    ataque[e]   cuánto marca el equipo e respecto a la media (1,0 = media)
    defensa[e]  cuánto encaja; por debajo de 1,0 es defensa buena
    """
    media_local: float = 1.4
    media_visitante: float = 1.1
    ataque: dict[str, float] = field(default_factory=dict)
    defensa: dict[str, float] = field(default_factory=dict)
    iteraciones: int = 12
    suavizado: float = 6.0

    def entrenar(self, partidos: list[Partido]) -> None:
        """
        Estima ataque y defensa alternando ambos hasta que se estabilizan.

        El suavizado tira de los equipos con pocos partidos hacia la media:
        sin él, un equipo recién ascendido con tres partidos raros quedaría
        con una fuerza absurda.
        """
        if not partidos:
            return

        self.media_local = sum(p.goles_local for p in partidos) / len(partidos)
        self.media_visitante = sum(p.goles_visitante for p in partidos) / len(partidos)

        equipos = {p.local for p in partidos} | {p.visitante for p in partidos}
        self.ataque = {e: 1.0 for e in equipos}
        self.defensa = {e: 1.0 for e in equipos}

        for _ in range(self.iteraciones):
            marcados = defaultdict(float)
            esperados_at = defaultdict(float)
            encajados = defaultdict(float)
            esperados_df = defaultdict(float)

            for p in partidos:
                el = self.media_local * self.ataque[p.local] * self.defensa[p.visitante]
                ev = self.media_visitante * self.ataque[p.visitante] * self.defensa[p.local]

                marcados[p.local] += p.goles_local
                marcados[p.visitante] += p.goles_visitante
                esperados_at[p.local] += el / max(self.ataque[p.local], 1e-6)
                esperados_at[p.visitante] += ev / max(self.ataque[p.visitante], 1e-6)

                encajados[p.visitante] += p.goles_local
                encajados[p.local] += p.goles_visitante
                esperados_df[p.visitante] += el / max(self.defensa[p.visitante], 1e-6)
                esperados_df[p.local] += ev / max(self.defensa[p.local], 1e-6)

            for e in equipos:
                if esperados_at[e] > 0:
                    bruto = marcados[e] / esperados_at[e]
                    peso = esperados_at[e] / (esperados_at[e] + self.suavizado)
                    self.ataque[e] = peso * bruto + (1 - peso) * 1.0
                if esperados_df[e] > 0:
                    bruto = encajados[e] / esperados_df[e]
                    peso = esperados_df[e] / (esperados_df[e] + self.suavizado)
                    self.defensa[e] = peso * bruto + (1 - peso) * 1.0

    def lambdas(self, local: str, visitante: str) -> tuple[float, float]:
        al = self.ataque.get(local, 1.0)
        av = self.ataque.get(visitante, 1.0)
        dl = self.defensa.get(local, 1.0)
        dv = self.defensa.get(visitante, 1.0)
        return (
            max(self.media_local * al * dv, 0.05),
            max(self.media_visitante * av * dl, 0.05),
        )

    @staticmethod
    def _distribucion(lam: float) -> dict[str, float]:
        """P(0), P(1), P(2) y P(3 o más) para una Poisson de media lam."""
        p0 = math.exp(-lam)
        p1 = lam * p0
        p2 = lam * lam * p0 / 2
        return {"0": p0, "1": p1, "2": p2, "M": max(1.0 - p0 - p1 - p2, 1e-9)}

    def predecir(self, local: str, visitante: str) -> dict[str, dict[str, float]]:
        """Distribución de goles de cada equipo por separado."""
        ll, lv = self.lambdas(local, visitante)
        return {"local": self._distribucion(ll), "visitante": self._distribucion(lv)}

    def probabilidad_pleno(
        self, local: str, visitante: str, marcado_local: str, marcado_visitante: str
    ) -> float:
        """
        Probabilidad de acertar el Pleno con una combinación concreta.

        Se multiplica la de cada equipo asumiendo independencia, que es la
        simplificación de este modelo.
        """
        d = self.predecir(local, visitante)
        return d["local"][marcado_local] * d["visitante"][marcado_visitante]


def categoria_real(goles: int) -> str:
    """Convierte un marcador real a la categoría del boleto."""
    return str(goles) if goles <= 2 else "M"


def evaluar(modelo: ModeloGoles, partidos: list[Partido]) -> dict:
    """
    Mide el modelo sobre partidos no vistos.

    Se comparan dos cosas frente a la referencia de predecir siempre la
    frecuencia base: si acierta la categoría más probable, y el log-loss,
    que penaliza estar seguro y equivocarse.
    """
    if not partidos:
        return {}

    frec_l = defaultdict(int)
    frec_v = defaultdict(int)
    for p in partidos:
        frec_l[categoria_real(p.goles_local)] += 1
        frec_v[categoria_real(p.goles_visitante)] += 1

    n = len(partidos)
    base_l = {c: max(frec_l[c] / n, 1e-9) for c in CATEGORIAS}
    base_v = {c: max(frec_v[c] / n, 1e-9) for c in CATEGORIAS}

    ok_l = ok_v = ok_ambos = 0
    ll = ll_base = 0.0

    for p in partidos:
        d = modelo.predecir(p.local, p.visitante)
        cl = categoria_real(p.goles_local)
        cv = categoria_real(p.goles_visitante)

        acl = max(d["local"], key=d["local"].get) == cl
        acv = max(d["visitante"], key=d["visitante"].get) == cv
        ok_l += acl
        ok_v += acv
        ok_ambos += acl and acv

        ll -= math.log(max(d["local"][cl], 1e-9)) + math.log(max(d["visitante"][cv], 1e-9))
        ll_base -= math.log(base_l[cl]) + math.log(base_v[cv])

    return {
        "n": n,
        "acierto_local": ok_l / n,
        "acierto_visitante": ok_v / n,
        "acierto_ambos": ok_ambos / n,
        "logloss": ll / (2 * n),
        "logloss_base": ll_base / (2 * n),
        "mejora_sobre_base": (ll_base - ll) / (2 * n),
        "frecuencias_local": {c: frec_l[c] / n for c in CATEGORIAS},
        "frecuencias_visitante": {c: frec_v[c] / n for c in CATEGORIAS},
    }
