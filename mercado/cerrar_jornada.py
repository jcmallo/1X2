"""
Cierra una jornada: escribe el resultado oficial de cada casilla.

POR QUÉ HACE FALTA

`signo_oficial` solo lo escribían los dos importadores del histórico
—Quinielandia y SELAE—, que se usan una vez para cargar años pasados.
Ninguna tarea semanal lo tocaba.

Consecuencia: una jornada recién jugada no se cerraba nunca. El desplegable
del panel construye la combinación ganadora con ese campo, así que la
jornada 4 de 2026-27 seguía apareciendo como pendiente el martes siguiente,
con los partidos ya jugados y el premio cobrado.

DE DÓNDE SALE EL RESULTADO

De nuestra propia tabla de partidos, que se actualiza dos veces al día. El
boleto ya está vinculado con ellos, así que basta mirar el marcador y
traducirlo a 1, X o 2. No hace falta volver a leer nada de LAE.

El Pleno al 15 va aparte: no se juega a 1X2 sino a goles de cada equipo,
con la categoría M para tres o más. Se escribe como 'L-V', por ejemplo
'0-1' o 'M-2'.

CUÁNDO SE EJECUTA

El lunes por la mañana, después del último partido del fin de semana. Si
algún partido se aplazó y aún no tiene marcador, esa casilla se deja sin
tocar y se vuelve a intentar al día siguiente: media jornada cerrada sería
peor que ninguna, porque el panel la daría por resuelta.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))

from api_client import ApiIngesta  # noqa: E402

CATEGORIAS = ("0", "1", "2", "M")


def signo(goles_local: int, goles_visitante: int) -> str:
    if goles_local > goles_visitante:
        return "1"
    if goles_local == goles_visitante:
        return "X"
    return "2"


def categoria(goles: int) -> str:
    """La casilla 15 se juega a goles: 0, 1, 2 o M para tres o más."""
    return str(goles) if goles < 3 else "M"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--temporada", default="", help="p.ej. 2026-27")
    p.add_argument("--jornada", type=int, default=0)
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    args = p.parse_args()

    api = ApiIngesta()

    datos = api.contexto_dashboard(
        temporada=args.temporada or None,
        numero_jornada=args.jornada or None,
    )
    j = datos.get("jornada")
    if not j:
        print("No hay ninguna jornada cargada.")
        return 1

    print(f"Jornada {j['numero']} · {j['temporada']}")

    casillas = []
    sin_jugar = []
    ya_cerradas = 0

    for c in sorted(datos.get("casillas", []), key=lambda x: int(x["posicion"])):
        pos = int(c["posicion"])
        gl, gv = c.get("goles_local"), c.get("goles_visitante")
        etq = f"{c.get('local')} - {c.get('visitante')}"

        if gl is None or gv is None:
            sin_jugar.append(f"{pos}. {etq}")
            continue

        gl, gv = int(gl), int(gv)
        casilla = {"posicion": pos}

        if pos == 15:
            valor = f"{categoria(gl)}-{categoria(gv)}"
            if c.get("signo_pleno_oficial") == valor:
                ya_cerradas += 1
            casilla["signo_pleno_oficial"] = valor
            print(f"  {pos:>2}. {etq:<44} {gl}-{gv}  →  Pleno {valor}")
        else:
            valor = signo(gl, gv)
            if c.get("signo_oficial") == valor:
                ya_cerradas += 1
            casilla["signo_oficial"] = valor
            print(f"  {pos:>2}. {etq:<44} {gl}-{gv}  →  {valor}")

        casillas.append(casilla)

    if sin_jugar:
        print(f"\n  {len(sin_jugar)} casillas sin marcador todavía:")
        for x in sin_jugar:
            print(f"    {x}")
        print("\n  No se cierra la jornada a medias: el panel la daría por")
        print("  resuelta y la combinación saldría incompleta. Se reintenta")
        print("  mañana, que es lo que pasa con un partido aplazado.")
        return 0

    if len(casillas) < 15:
        print(f"\n  Solo {len(casillas)} casillas de 15. No se cierra.")
        return 0

    if ya_cerradas == len(casillas):
        print("\n  Ya estaba cerrada con estos mismos resultados. Nada que hacer.")
        return 0

    if args.dry_run:
        print("\nDRY RUN: no se ha escrito nada.")
        return 0

    r = api.importar_jornada_historica({
        "numero_jornada": int(j["numero"]),
        "etiqueta_temporada": j["temporada"],
        "fuente": "partidos",
        "fuente_fichero": "cerrar_jornada",
        "casillas": casillas,
    })
    print(f"\nCerrada: {r.get('accion')}, jornada_id {r.get('jornada_id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
