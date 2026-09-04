"""
Importa el histórico de Quinielandia a MariaDB.

Fuente: ValoracionesCoeficiente_BtF_LAE.xlsb
  hoja 'Valoraciones'  1.010 jornadas x 15 casillas, con signo real,
                       probabilidad BetFair y proporción apostada LAE
  hoja 'Resumen'       1.010 escrutinios: recaudación, acertantes, premios

Uso:
    python importar_quinielandia.py --fichero ruta/al.xlsb --dry-run
    python importar_quinielandia.py --fichero ruta/al.xlsb --limite 5
    python importar_quinielandia.py --fichero ruta/al.xlsb

Variables de entorno:
    INGEST_API_URL, INGEST_API_TOKEN

Requiere: pip install pyxlsb
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
from api_client import ApiIngesta  # noqa: E402

try:
    import pyxlsb
except ImportError:
    print("Falta pyxlsb. Instalar con: pip install pyxlsb")
    sys.exit(1)


PATRON_JORNADA = re.compile(r"^(\d{4})/(\d{2})_(\d+)$")

# Columnas de la hoja 'Resumen', por posición.
COLS_RESUMEN = {
    "precio_apuesta": 3,
    "pct_categoria_14": 4,
    "recaudacion": 5,
    "apuestas_validadas": 6,
    "acertantes_14": 7,
    "premio_14": 8,
    "premio_14_normalizado": 9,
    # La columna 10 ('P.Res.BtF.') no se importa: su escala no es consistente
    # entre filas. La probabilidad se deriva de la información: p = 3^(-I).
    "informacion_mercado": 11,
    "informacion_lae": 13,
    "coeficiente": 14,
    "coeficiente_absoluto": 15,
    "coeficiente_mayor_1": 16,
    "coeficiente_abs_mayor_1": 17,
    "em_14": 19,
    "em_14_absoluto": 20,
}


@dataclass
class Casilla:
    posicion: int
    local: str
    visitante: str
    signo: str | None
    mercado: dict | None = None
    lae: dict | None = None


@dataclass
class Jornada:
    numero: int
    temporada: str
    casillas: list[Casilla] = field(default_factory=list)
    escrutinio: dict | None = None

    @property
    def clave(self) -> str:
        return f"{self.temporada}_{self.numero:02d}"


# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------

def partir_partido(nombre: str) -> tuple[str, str] | None:
    """
    Separa 'LOCAL - VISITANTE' en sus dos equipos.

    El separador no es uniforme en el fichero: hay 'A - B', 'A- B' y 'A-B'.
    Además algunos clubes llevan guion propio ('HAM-KAM', 'SINT-TRUIDEN'),
    así que se prueba primero el separador inequívoco con espacios a ambos
    lados y solo después los demás.
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


def normalizar_temporada(bruta: str) -> str:
    """'2009/10' -> '2009-10', para casar con nucleo_temporadas."""
    return bruta.replace("/", "-")


def normalizar_signo(valor) -> str | None:
    """La columna de signo mezcla números (1.0, 2.0) y texto ('X')."""
    if valor is None:
        return None
    if isinstance(valor, str):
        s = valor.strip().upper()
        return s if s in {"1", "X", "2"} else None
    if isinstance(valor, (int, float)):
        entero = int(valor)
        return {1: "1", 2: "2"}.get(entero)
    return None


def terna(fila: list, c1: int, cx: int, c2: int) -> dict | None:
    """
    Extrae una terna de porcentajes y la pasa a fracciones 0..1.

    Devuelve None si falta algún valor o si no suman ~100: es preferible no
    importar una probabilidad que importarla mal.
    """
    try:
        vals = [fila[c1], fila[cx], fila[c2]]
    except IndexError:
        return None

    if any(v is None or not isinstance(v, (int, float)) for v in vals):
        return None

    p1, px, p2 = (float(v) for v in vals)
    suma = p1 + px + p2
    if suma < 95.0 or suma > 105.0:
        return None

    return {
        "p1": round(p1 / 100, 6),
        "px": round(px / 100, 6),
        "p2": round(p2 / 100, 6),
    }


def leer_valoraciones(filas: list[list]) -> list[Jornada]:
    """Recorre la hoja en bloques de jornada y extrae las 15 casillas."""
    cabeceras = []
    for i, f in enumerate(filas):
        for c in f:
            if isinstance(c, str) and PATRON_JORNADA.match(c.strip()):
                cabeceras.append((i, c.strip()))
                break

    jornadas = []
    for k, (ini, etiqueta) in enumerate(cabeceras):
        fin = cabeceras[k + 1][0] if k + 1 < len(cabeceras) else len(filas)

        m = PATRON_JORNADA.match(etiqueta)
        temporada = normalizar_temporada(f"{m.group(1)}/{m.group(2)}")
        numero = int(m.group(3))

        jornada = Jornada(numero=numero, temporada=temporada)

        for r in range(ini, fin):
            fila = filas[r]
            if len(fila) < 4:
                continue

            pos = fila[1]
            if not isinstance(pos, (int, float)) or not (1 <= pos <= 15):
                continue

            nombre = fila[2]
            if not isinstance(nombre, str):
                continue

            equipos = partir_partido(nombre)
            if equipos is None:
                continue

            jornada.casillas.append(
                Casilla(
                    posicion=int(pos),
                    local=equipos[0][:100],
                    visitante=equipos[1][:100],
                    signo=normalizar_signo(fila[3]),
                    mercado=terna(fila, 4, 5, 6),
                    lae=terna(fila, 8, 9, 10),
                )
            )

        jornadas.append(jornada)

    return jornadas


def leer_resumen(filas: list[list]) -> dict[str, dict]:
    """Devuelve el escrutinio de cada jornada, indexado por '2009-10_01'."""
    salida = {}

    for fila in filas[1:]:
        if len(fila) < 3 or not isinstance(fila[2], str):
            continue

        m = PATRON_JORNADA.match(fila[2].strip())
        if not m:
            continue

        clave = (
            normalizar_temporada(f"{m.group(1)}/{m.group(2)}")
            + f"_{int(m.group(3)):02d}"
        )

        datos = {}
        for campo, col in COLS_RESUMEN.items():
            if col < len(fila) and isinstance(fila[col], (int, float)):
                datos[campo] = float(fila[col])

        # Estos dos son recuentos, no importes.
        for entero in ("apuestas_validadas", "acertantes_14"):
            if entero in datos:
                datos[entero] = int(datos[entero])

        salida[clave] = datos

    return salida


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importa el histórico de Quinielandia a MariaDB."
    )
    parser.add_argument("--fichero", required=True, help="ruta al .xlsb")
    parser.add_argument(
        "--dry-run", action="store_true", help="no escribe, solo informa"
    )
    parser.add_argument(
        "--limite", type=int, default=0, help="importar solo N jornadas"
    )
    parser.add_argument(
        "--desde", default="", help="temporada inicial, p.ej. 2022-23"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.fichero):
        print(f"No existe el fichero: {args.fichero}")
        return 1

    nombre_fichero = os.path.basename(args.fichero)

    print(f"Leyendo {nombre_fichero}...")
    with pyxlsb.open_workbook(args.fichero) as wb:
        with wb.get_sheet("Valoraciones") as s:
            filas_val = [[c.v for c in f] for f in s.rows()]
        with wb.get_sheet("Resumen") as s:
            filas_res = [[c.v for c in f] for f in s.rows()]

    jornadas = leer_valoraciones(filas_val)
    escrutinios = leer_resumen(filas_res)

    print(f"  jornadas en Valoraciones: {len(jornadas)}")
    print(f"  escrutinios en Resumen:   {len(escrutinios)}")

    for j in jornadas:
        j.escrutinio = escrutinios.get(j.clave)

    # --- Claves repetidas -------------------------------------------------
    #
    # El fichero de origen tiene al menos un caso (2017-18_38) en el que dos
    # jornadas completamente distintas comparten etiqueta: una de Champions y
    # otra de liga española. Importar las dos haría que la segunda pisara a la
    # primera sin avisar, y elegir una al azar sería inventarse cuál lleva el
    # número correcto. Se apartan y se informa para decidirlo a mano.

    vistas: dict[str, list[Jornada]] = {}
    for j in jornadas:
        vistas.setdefault(j.clave, []).append(j)

    repetidas = {k: v for k, v in vistas.items() if len(v) > 1}
    jornadas = [v[0] for v in vistas.values() if len(v) == 1]

    if repetidas:
        print()
        print(f"APARTADAS por etiqueta repetida ({len(repetidas)}):")
        for clave, grupo in repetidas.items():
            print(f"  {clave} aparece {len(grupo)} veces:")
            for n, j in enumerate(grupo, 1):
                muestra = ", ".join(
                    f"{c.local}-{c.visitante}" for c in j.casillas[:3]
                )
                print(f"    {n}) {muestra}...")
        print("  No se importan. Corregir la etiqueta en origen para incluirlas.")

    if args.desde:
        jornadas = [j for j in jornadas if j.temporada >= args.desde]
        print(f"  filtradas desde {args.desde}: {len(jornadas)}")

    if args.limite:
        jornadas = jornadas[: args.limite]

    # --- Comprobaciones antes de escribir --------------------------------

    incompletas = [j for j in jornadas if len(j.casillas) != 15]
    sin_escrutinio = [j for j in jornadas if j.escrutinio is None]
    sin_signos = [j for j in jornadas if not any(c.signo for c in j.casillas)]

    total_casillas = sum(len(j.casillas) for j in jornadas)
    con_mercado = sum(1 for j in jornadas for c in j.casillas if c.mercado)
    con_lae = sum(1 for j in jornadas for c in j.casillas if c.lae)

    print()
    print("Cobertura:")
    print(f"  casillas totales:          {total_casillas}")
    print(f"  con probabilidad mercado:  {con_mercado}")
    print(f"  con proporción LAE:        {con_lae}")
    print(f"  jornadas sin 15 casillas:  {len(incompletas)}")
    print(f"  jornadas sin escrutinio:   {len(sin_escrutinio)}")
    print(f"  jornadas sin ningún signo: {len(sin_signos)}")

    if incompletas:
        print("\n  Jornadas incompletas:")
        for j in incompletas[:10]:
            print(f"    {j.clave}: {len(j.casillas)} casillas")

    if args.dry_run:
        print("\nDRY RUN: no se ha escrito nada.")
        if jornadas:
            j = jornadas[0]
            print(f"\nEjemplo — {j.clave}:")
            for c in j.casillas[:4]:
                m = c.mercado
                l = c.lae
                sm = f"{m['p1']:.3f}/{m['px']:.3f}/{m['p2']:.3f}" if m else "-"
                sl = f"{l['p1']:.3f}/{l['px']:.3f}/{l['p2']:.3f}" if l else "-"
                print(
                    f"  {c.posicion:2}. {c.local:<16} - {c.visitante:<16}"
                    f"  [{c.signo}]  mercado {sm}  lae {sl}"
                )
            if j.escrutinio:
                print(f"  escrutinio: {j.escrutinio}")
        return 0

    # --- Importación ------------------------------------------------------

    api = ApiIngesta()
    creadas = actualizadas = fallidas = 0

    print(f"\nImportando {len(jornadas)} jornadas...")

    for n, j in enumerate(jornadas, 1):
        payload = {
            "numero_jornada": j.numero,
            "etiqueta_temporada": j.temporada,
            "fuente": "quinielandia",
            "fuente_fichero": nombre_fichero,
            "casillas": [
                {
                    "posicion": c.posicion,
                    "equipo_local_impreso": c.local,
                    "equipo_visitante_impreso": c.visitante,
                    **({"signo_oficial": c.signo} if c.signo else {}),
                    **({"prob_mercado": c.mercado} if c.mercado else {}),
                    **({"prob_lae": c.lae} if c.lae else {}),
                }
                for c in j.casillas
            ],
        }
        if j.escrutinio:
            payload["escrutinio"] = j.escrutinio

        try:
            r = api.importar_jornada_historica(payload)
            if r.get("accion") == "creada":
                creadas += 1
            else:
                actualizadas += 1
        except RuntimeError as exc:
            fallidas += 1
            print(f"  fallo en {j.clave}: {exc}")

        if n % 50 == 0 or n == len(jornadas):
            print(
                f"  {n}/{len(jornadas)}  "
                f"creadas {creadas}, actualizadas {actualizadas}, "
                f"fallidas {fallidas}"
            )

    print()
    print(f"Creadas:      {creadas}")
    print(f"Actualizadas: {actualizadas}")
    print(f"Fallidas:     {fallidas}")

    return 0 if fallidas == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
