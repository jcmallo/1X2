"""
Carga del histórico de Quinielandia en estructuras de trabajo.

Cruza los dos ficheros de origen:

  ValoracionesCoeficiente_BtF_LAE.xlsb
      probabilidad de mercado (BetFair) y proporción apostada (LAE),
      por casilla, desde 2009/10

  Estadistica Real.xlsb
      'Historico Resultados'  combinación ganadora, desde 1948/49
      'Premios Unitarios'     premio real de cada categoría y precio de
                              la apuesta, desde 1990/91

La intersección de los tres es lo único con lo que se puede hacer
backtesting económico: 992 jornadas entre 2009/10 y 2025/26.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pyxlsb


PATRON_JORNADA = re.compile(r"^(\d{4})/(\d{2})_(\d+)$")

# Columnas de la hoja 'Premios Unitarios': categoría -> columna del premio
# unitario (lo que cobró cada acertante de esa categoría).
COL_PREMIO = {10: 47, 11: 41, 12: 35, 13: 9, 14: 19}
COL_ACERTANTES = {10: 44, 11: 38, 12: 32, 13: 6, 14: 16}
COL_RECAUDACION = 1
COL_APUESTAS = 2
COL_PRECIO = 50

SIGNOS = ("1", "X", "2")
CLAVE_PROB = {"1": "p1", "X": "px", "2": "p2"}


@dataclass
class Casilla:
    """Una de las 15 casillas del boleto."""
    posicion: int
    local: str
    visitante: str
    signo: str | None
    mercado: dict | None = None   # {p1, px, p2} fracciones 0..1
    lae: dict | None = None


@dataclass
class JornadaHistorica:
    numero: int
    temporada: str                # '2009-10'
    casillas: list[Casilla] = field(default_factory=list)
    resultado: str | None = None  # 14 signos, p.ej. '1X21112X111121'
    precio: float | None = None
    recaudacion: float | None = None
    apuestas: int | None = None
    premios: dict = field(default_factory=dict)  # {10: eur, ..., 14: eur}

    @property
    def clave(self) -> str:
        return f"{self.temporada}_{self.numero:02d}"

    @property
    def clave_origen(self) -> str:
        """Como aparece en los .xlsb: '2009/10_01'."""
        return self.clave.replace("-", "/")

    @property
    def completa(self) -> bool:
        """Tiene todo lo necesario para un backtest económico."""
        return (
            self.resultado is not None
            and self.precio is not None
            and len(self.casillas_jugables) == 14
        )

    @property
    def casillas_jugables(self) -> list[Casilla]:
        """
        Las 14 primeras casillas con ambas probabilidades.

        La 15 se excluye: es el Pleno al 15, que no se apuesta como 1X2 y
        por tanto no tiene proporción LAE.
        """
        return sorted(
            (
                c for c in self.casillas
                if c.posicion <= 14 and c.mercado and c.lae
            ),
            key=lambda c: c.posicion,
        )


# ---------------------------------------------------------------------------
# Lectura de ficheros
# ---------------------------------------------------------------------------

def _partir_partido(nombre: str) -> tuple[str, str] | None:
    """
    Separa 'LOCAL - VISITANTE'.

    El separador no es uniforme ('A - B', 'A- B', 'A-B') y algunos clubes
    llevan guion propio ('HAM-KAM'), así que se prueba primero el separador
    con espacios a ambos lados, que es inequívoco.
    """
    n = nombre.strip()
    if " - " in n:
        a, b = n.split(" - ", 1)
        return a.strip(), b.strip()
    m = re.match(r"^(.+?)\s*-\s+(.+)$", n) or re.match(r"^(.+?)\s+-\s*(.+)$", n)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if "-" in n:
        a, b = n.split("-", 1)
        return a.strip(), b.strip()
    return None


def _normalizar_signo(valor) -> str | None:
    """La columna de signo mezcla números (1.0, 2.0) y texto ('X')."""
    if isinstance(valor, str):
        s = valor.strip().upper()
        return s if s in SIGNOS else None
    if isinstance(valor, (int, float)):
        return {1: "1", 2: "2"}.get(int(valor))
    return None


def _terna(fila: list, c1: int, cx: int, c2: int) -> dict | None:
    """Porcentajes -> fracciones. None si faltan o no suman ~100."""
    try:
        vals = [fila[c1], fila[cx], fila[c2]]
    except IndexError:
        return None
    if any(v is None or not isinstance(v, (int, float)) for v in vals):
        return None
    p1, px, p2 = (float(v) for v in vals)
    if not (95.0 <= p1 + px + p2 <= 105.0):
        return None
    return {"p1": p1 / 100, "px": px / 100, "p2": p2 / 100}


def cargar(ruta_valoraciones: str, ruta_estadistica: str) -> list[JornadaHistorica]:
    """
    Devuelve las jornadas con todo lo disponible cruzado.

    Aparta las que comparten etiqueta: en el fichero de origen hay al menos
    un caso (2017/18_38) donde dos jornadas distintas llevan el mismo
    identificador, y quedarse con una sería elegir al azar cuál es la buena.
    """
    with pyxlsb.open_workbook(ruta_valoraciones) as wb:
        with wb.get_sheet("Valoraciones") as s:
            filas_val = [[c.v for c in f] for f in s.rows()]

    with pyxlsb.open_workbook(ruta_estadistica) as wb:
        with wb.get_sheet("Historico Resultados") as s:
            filas_res = [[c.v for c in f] for f in s.rows()]
        with wb.get_sheet("Premios Unitarios") as s:
            filas_pre = [[c.v for c in f] for f in s.rows()]

    # Resultados: solo combinaciones de 14 signos válidos
    resultados = {}
    for f in filas_res[3:]:
        if len(f) > 2 and isinstance(f[1], str) and isinstance(f[2], str):
            r = f[2].strip()
            if len(r) == 14 and set(r) <= set("1X2"):
                resultados[f[1].strip()] = r

    # Premios y recaudación
    economico = {}
    for f in filas_pre[1:]:
        if not (f and isinstance(f[0], str)):
            continue
        clave = f[0].strip()
        if not PATRON_JORNADA.match(clave):
            continue

        def num(col):
            return (
                float(f[col])
                if len(f) > col and isinstance(f[col], (int, float))
                else None
            )

        economico[clave] = {
            "precio": num(COL_PRECIO),
            "recaudacion": num(COL_RECAUDACION),
            "apuestas": num(COL_APUESTAS),
            "premios": {c: (num(col) or 0.0) for c, col in COL_PREMIO.items()},
        }

    # Casillas, por bloques de jornada
    cabeceras = []
    for i, f in enumerate(filas_val):
        for c in f:
            if isinstance(c, str) and PATRON_JORNADA.match(c.strip()):
                cabeceras.append((i, c.strip()))
                break

    por_clave: dict[str, list[JornadaHistorica]] = {}

    for k, (ini, etiqueta) in enumerate(cabeceras):
        fin = cabeceras[k + 1][0] if k + 1 < len(cabeceras) else len(filas_val)
        m = PATRON_JORNADA.match(etiqueta)

        j = JornadaHistorica(
            numero=int(m.group(3)),
            temporada=f"{m.group(1)}-{m.group(2)}",
        )

        for r in range(ini, fin):
            fila = filas_val[r]
            if len(fila) < 4:
                continue
            pos = fila[1]
            if not isinstance(pos, (int, float)) or not (1 <= pos <= 15):
                continue
            if not isinstance(fila[2], str):
                continue
            equipos = _partir_partido(fila[2])
            if equipos is None:
                continue

            j.casillas.append(
                Casilla(
                    posicion=int(pos),
                    local=equipos[0],
                    visitante=equipos[1],
                    signo=_normalizar_signo(fila[3]),
                    mercado=_terna(fila, 4, 5, 6),
                    lae=_terna(fila, 8, 9, 10),
                )
            )

        clave_origen = etiqueta
        j.resultado = resultados.get(clave_origen)
        eco = economico.get(clave_origen)
        if eco:
            j.precio = eco["precio"]
            j.recaudacion = eco["recaudacion"]
            j.apuestas = int(eco["apuestas"]) if eco["apuestas"] else None
            j.premios = eco["premios"]

        por_clave.setdefault(j.clave, []).append(j)

    return [v[0] for v in por_clave.values() if len(v) == 1]


def solo_completas(jornadas: list[JornadaHistorica]) -> list[JornadaHistorica]:
    """Las que sirven para backtesting económico."""
    return [j for j in jornadas if j.completa]
