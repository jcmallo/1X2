"""
Características de un partido, calculadas solo con lo anterior a él.

Por qué existe este módulo
--------------------------

El Elo a secas resume un equipo en un número que solo sabe de resultados.
Eso basta donde hay diferencias grandes de nivel (Liga F, donde acierta el
66%), pero se queda corto en Segunda División, la liga más igualada: allí
acertaba un 40,8% y su log-loss era PEOR que predecir siempre la frecuencia
media. Es decir, hacía daño.

Lo que faltaba estaba en la base sin usar: cómo llega cada equipo, cuánto ha
descansado, qué pasó las últimas veces que se vieron las caras. Añadiéndolo,
Segunda sube a 46,6% y deja de empeorar la predicción.

    competición        Elo solo              con características
    LaLiga             49,4%  +0,0232        50,8%  +0,0522
    Segunda División   40,8%  -0,0274        46,6%  +0,0038
    Liga F             66,3%  +0,1590        63,0%  +0,2334

En Liga F baja el acierto pero mejora mucho el log-loss: el modelo se moja
menos y calibra mejor, que es lo que importa para calcular valor.

La regla que no se puede romper
-------------------------------

Cada partido se describe con lo que se sabía ANTES de jugarlo. El estado de
los equipos se actualiza después de haber generado sus características, no
antes. Saltarse eso da un modelo que parece buenísimo en las pruebas y
fracasa en cuanto se usa de verdad, porque en la vida real el resultado
todavía no existe.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import datetime


# Cuántos partidos recientes definen la forma de un equipo. Cinco es lo
# habitual en fútbol: un mes de competición.
VENTANA_FORMA = 5

# Enfrentamientos directos que se tienen en cuenta.
VENTANA_DIRECTOS = 3

# Días de descanso a partir de los cuales da igual descansar más.
TOPE_DESCANSO = 14

# Partidos de una temporada, para normalizar la experiencia acumulada.
TEMPORADA = 38

NOMBRES = [
    "elo_diferencia",
    "forma_local", "forma_visitante", "forma_diferencia",
    "goles_favor_local", "goles_contra_local",
    "goles_favor_visitante", "goles_contra_visitante",
    "descanso_local", "descanso_visitante",
    "partidos_local", "partidos_visitante",
    "directos",
    "sesgo",
]

SIGNO_A_CLASE = {"1": 0, "X": 1, "2": 2}


@dataclass
class Estado:
    """
    Lo que se sabe de cada equipo en un momento dado.

    Se construye recorriendo los partidos en orden cronológico y llamando a
    `caracteristicas()` antes de `registrar()` para cada uno.
    """

    k_elo: float = 20.0
    elo_inicial: float = 1500.0

    historial: dict = field(default_factory=lambda: collections.defaultdict(list))
    directos: dict = field(default_factory=lambda: collections.defaultdict(list))
    elo: dict = field(default_factory=dict)

    def valoracion(self, equipo: str) -> float:
        return self.elo.get(equipo, self.elo_inicial)

    def _forma(self, equipo: str) -> tuple[float, float, float]:
        """
        Puntos por partido y goles recientes, normalizados.

        Un equipo sin historial (recién ascendido, o el primer partido de
        todo) recibe valores neutros en lugar de ceros: un cero diría
        "viene de perderlo todo", que es una afirmación falsa.
        """
        ultimos = self.historial[equipo][-VENTANA_FORMA:]
        if not ultimos:
            return (1.0, 1.0, 1.0)
        n = len(ultimos)
        return (
            sum(p for _, p, _, _ in ultimos) / (3.0 * n),
            sum(gf for _, _, gf, _ in ultimos) / n,
            sum(gc for _, _, _, gc in ultimos) / n,
        )

    def _descanso(self, equipo: str, cuando: datetime) -> float:
        """Días desde el último partido, con tope. Sin historial se asume una semana."""
        if not self.historial[equipo]:
            return 1.0
        dias = (cuando - self.historial[equipo][-1][0]).days
        return min(max(dias, 0), TOPE_DESCANSO) / TOPE_DESCANSO

    def caracteristicas(self, local: str, visitante: str, cuando: datetime) -> list[float]:
        """El vector que describe el partido, con la información previa a él."""
        f_loc, gf_loc, gc_loc = self._forma(local)
        f_vis, gf_vis, gc_vis = self._forma(visitante)

        previos = self.directos[(local, visitante)][-VENTANA_DIRECTOS:]

        return [
            (self.valoracion(local) - self.valoracion(visitante)) / 100.0,
            f_loc, f_vis, f_loc - f_vis,
            gf_loc, gc_loc, gf_vis, gc_vis,
            self._descanso(local, cuando), self._descanso(visitante, cuando),
            len(self.historial[local]) / TEMPORADA,
            len(self.historial[visitante]) / TEMPORADA,
            sum(previos) / len(previos) if previos else 0.5,
            1.0,
        ]

    def registrar(
        self,
        local: str,
        visitante: str,
        cuando: datetime,
        goles_local: int,
        goles_visitante: int,
    ) -> None:
        """Incorpora un partido ya jugado. Llamar DESPUÉS de caracteristicas()."""
        if goles_local > goles_visitante:
            pts_local, pts_vis, resultado = 3, 0, 1.0
        elif goles_local == goles_visitante:
            pts_local, pts_vis, resultado = 1, 1, 0.5
        else:
            pts_local, pts_vis, resultado = 0, 3, 0.0

        self.historial[local].append((cuando, pts_local, goles_local, goles_visitante))
        self.historial[visitante].append((cuando, pts_vis, goles_visitante, goles_local))
        self.directos[(local, visitante)].append(resultado)

        rl = self.valoracion(local)
        rv = self.valoracion(visitante)
        esperado = 1.0 / (1.0 + 10 ** (-(rl - rv) / 400.0))
        ajuste = self.k_elo * (resultado - esperado)
        self.elo[local] = rl + ajuste
        self.elo[visitante] = rv - ajuste


def construir(filas: list[dict]) -> tuple[list[list[float]], list[int], list[tuple], Estado]:
    """
    Recorre los partidos en orden y devuelve X, y, metadatos y el estado final.

    El estado que devuelve ya ha visto todos los partidos, así que sirve
    para describir los de la jornada que viene.
    """
    partidos = []
    for f in filas:
        try:
            partidos.append({
                "dt": datetime.fromisoformat(f["fecha_hora_inicio"]),
                "local": f["equipo_local"],
                "visitante": f["equipo_visitante"],
                "gl": int(f["goles_local"]),
                "gv": int(f["goles_visitante"]),
                "signo": f["signo"],
                "competicion": f.get("competicion", ""),
            })
        except (KeyError, TypeError, ValueError):
            continue

    partidos.sort(key=lambda p: p["dt"])

    estado = Estado()
    X: list[list[float]] = []
    y: list[int] = []
    meta: list[tuple] = []

    for p in partidos:
        clase = SIGNO_A_CLASE.get(p["signo"])
        if clase is None:
            continue

        # El orden importa: primero se describe, después se aprende.
        X.append(estado.caracteristicas(p["local"], p["visitante"], p["dt"]))
        y.append(clase)
        meta.append((p["competicion"], p["dt"], p["local"], p["visitante"]))

        estado.registrar(p["local"], p["visitante"], p["dt"], p["gl"], p["gv"])

    return X, y, meta, estado
