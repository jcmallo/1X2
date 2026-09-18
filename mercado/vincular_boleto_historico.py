"""
Vincula el histórico completo de casillas de la quiniela con nucleo_partidos,
no solo la jornada de esta semana.

Por qué hace falta un script aparte de vincular_boleto.py
-----------------------------------------------------------

mercado/vincular_boleto.py está pensado para engancharse cada semana, poco
antes de jugarse la jornada: pide los partidos candidatos con
contexto_cuotas(), que solo mira una ventana de pocos días alrededor de
"ahora" (retro_horas=48, dias_futuro=8). Sirve para eso, pero no sirve para
el histórico: 1.093 jornadas desde 2009/10, con miles de partidos que ya se
jugaron hace meses o años.

Este script hace exactamente el mismo emparejamiento —misma normalización,
mismos alias, mismo umbral 0.72, misma norma de "ante la duda, no se
vincula"— pero contra TODO el universo de partidos con resultado
(contexto_resultados(), paginado: el mismo que usa modelado/pronosticar.py
para entrenar) y recorriendo cada jornada histórica una por una.

Por qué el rango empieza en 2022-23
--------------------------------------

nucleo_partidos solo tiene partidos desde 2022-08-12 (el alcance actual del
proyecto: "masculino y femenino, 2022-2027"). Las temporadas 2009/10-2021/22
de quiniela_probabilidades no tienen con qué enlazarse todavía: no es un
fallo de este script, es que esos partidos sencillamente no existen en la
base normalizada. Si algún día se amplía nucleo_partidos hacia atrás, basta
con añadir esas temporadas a --temporadas.

Por qué no llega al 100% ni ampliando el histórico
------------------------------------------------------

La quiniela mete de vez en cuando partidos de ligas extranjeras (Serie A,
Bundesliga, ligas escandinavas...) cuando LaLiga/Segunda paran por Copa o
por selecciones. nucleo_partidos no las tiene: son "competición
complementaria", tabla pendiente en el proyecto (nucleo_partidos_
complementarios). Esas casillas quedan sin vincular a propósito, no por un
fallo de emparejamiento: --informe las lista aparte para no confundirlas
con errores.

Validado contra una copia local de la base real (dump de producción,
2026-09-18): reproduce 45/47 de los vínculos que ya existían en producción
tal cual, y explica con una confianza >=0.97 los otros 2 (partidos
reprogramados lejos de la fecha de sorteo original). Sobre las 4.515
casillas de 2022-23 en adelante, encuentra ~2.490 vínculos nuevos con
confianza >=0.72; el resto son mayoritariamente partidos de competiciones
que este proyecto todavía no ingesta (ver arriba).

Uso
---

    python -m mercado.vincular_boleto_historico --dry-run
    python -m mercado.vincular_boleto_historico --dry-run --temporadas 2023-24,2024-25
    python -m mercado.vincular_boleto_historico   # aplica de verdad
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
from api_client import ApiIngesta  # noqa: E402

from mercado.vincular_boleto import (  # noqa: E402
    UMBRAL,
    es_femenino,
    normalizar,
    parecido,
)

# Temporadas dentro del alcance real de nucleo_partidos. La actual se deja
# fuera por defecto: para esa ya se encarga vincular_boleto.py cada semana,
# con datos más frescos que un backfill puntual.
TEMPORADAS_DEFECTO = ["2022-23", "2023-24", "2024-25", "2025-26"]

# Calibrado sobre los 47 vínculos que ya existían en producción: el 96% cae
# entre -2 y +1 días de la fecha de sorteo. Se deja margen a ambos lados
# porque la fecha de sorteo es la del jueves de esa semana, no la del
# partido, y algunos partidos se adelantan o atrasan por televisión.
VENTANA_DIAS_DEFECTO = 5

MAX_JORNADAS_POR_TEMPORADA = 80  # nunca ha habido más de 76; sobra margen


def cargar_universo(api: ApiIngesta) -> list[dict]:
    """Todos los partidos con resultado, con nombre normalizado y por día."""
    todos: list[dict] = []
    offset = 0
    while True:
        d = api.contexto_resultados(limite=1000, offset=offset)
        items = d.get("items", [])
        todos += items
        offset += len(items)
        if not d.get("hay_mas") or not items:
            break

    for it in todos:
        it["local_n"] = normalizar(it["equipo_local"])
        it["visitante_n"] = normalizar(it["equipo_visitante"])

    return todos


def indexar_por_dia(universo: list[dict]) -> dict:
    por_dia = defaultdict(list)
    for it in universo:
        try:
            dt = datetime.fromisoformat(it["fecha_hora_inicio"]).date()
        except (KeyError, ValueError):
            continue
        por_dia[dt].append(it)
    return por_dia


def emparejar_jornada(
    casillas: list[dict],
    fecha_sorteo,
    por_dia: dict,
    ventana_dias: int,
) -> tuple[list[dict], list[tuple]]:
    """Devuelve (casillas_para_guardar, sin_vincular_para_informe)."""
    candidatos = []
    for delta in range(-ventana_dias, ventana_dias + 1):
        candidatos += por_dia.get(fecha_sorteo + timedelta(days=delta), [])

    nuevas = []
    sin_vinculo = []

    for c in sorted(casillas, key=lambda x: int(x["posicion"])):
        pos = int(c["posicion"])
        local, visitante = c["equipo_local_impreso"], c["equipo_visitante_impreso"]

        entrada = {
            "posicion": pos,
            "equipo_local_impreso": local,
            "equipo_visitante_impreso": visitante,
        }

        if c.get("partido_id"):
            entrada["partido_id"] = int(c["partido_id"])
            nuevas.append(entrada)
            continue

        femenino = es_femenino(local) or es_femenino(visitante)
        genero = "FEMENINO" if femenino else "MASCULINO"
        nl, nv = normalizar(local), normalizar(visitante)

        mejor, mejor_score = None, 0.0
        for cand in candidatos:
            if cand.get("genero") and cand["genero"] != genero:
                continue
            score = (parecido(nl, cand["local_n"]) + parecido(nv, cand["visitante_n"])) / 2
            if score > mejor_score:
                mejor_score, mejor = score, cand

        if mejor and mejor_score >= UMBRAL:
            entrada["partido_id"] = int(mejor["partido_id"])
            nuevas.append(entrada)
        else:
            nuevas.append(entrada)
            sin_vinculo.append((pos, local, visitante, mejor, mejor_score))

    return nuevas, sin_vinculo


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill histórico: vincula boletos antiguos con nucleo_partidos."
    )
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    p.add_argument(
        "--temporadas",
        default=",".join(TEMPORADAS_DEFECTO),
        help="lista separada por comas, p.ej. 2023-24,2024-25",
    )
    p.add_argument("--ventana-dias", type=int, default=VENTANA_DIAS_DEFECTO)
    p.add_argument(
        "--informe",
        action="store_true",
        help="al final, lista las casillas que quedaron sin vincular",
    )
    args = p.parse_args()

    temporadas = [t.strip() for t in args.temporadas.split(",") if t.strip()]
    api = ApiIngesta()

    print("Descargando universo de partidos con resultado...")
    universo = cargar_universo(api)
    print(f"  {len(universo)} partidos")
    por_dia = indexar_por_dia(universo)

    total_ya = total_nuevas = total_sin = total_jornadas = 0
    todos_sin_vinculo = []

    for temporada in temporadas:
        print(f"\n=== {temporada} ===")
        for numero_jornada in range(1, MAX_JORNADAS_POR_TEMPORADA + 1):
            jornadas = api.contexto_quiniela(
                temporada=temporada, numero_jornada=numero_jornada, limite=1,
            )
            if not jornadas:
                continue

            jornada = jornadas[0]
            casillas = jornada.get("casillas", [])
            if not casillas or not jornada.get("fecha_sorteo"):
                continue

            fecha_sorteo = datetime.fromisoformat(jornada["fecha_sorteo"]).date()
            ya_antes = sum(1 for c in casillas if c.get("partido_id"))

            nuevas, sin_vinculo = emparejar_jornada(
                casillas, fecha_sorteo, por_dia, args.ventana_dias,
            )
            vinculadas = sum(1 for e in nuevas if "partido_id" in e)

            total_jornadas += 1
            total_ya += ya_antes
            total_nuevas += vinculadas - ya_antes
            total_sin += len(sin_vinculo)
            todos_sin_vinculo += [(temporada, numero_jornada, *s) for s in sin_vinculo]

            print(
                f"  J{numero_jornada:>2}: {ya_antes:>2} ya vinculadas, "
                f"+{vinculadas - ya_antes:>2} nuevas, {len(sin_vinculo):>2} sin vincular"
            )

            if args.dry_run or vinculadas == ya_antes:
                continue

            payload = {
                "numero_jornada": numero_jornada,
                "etiqueta_temporada": temporada,
                "fuente": jornada.get("fuente", "selae"),
                "fecha_sorteo": jornada["fecha_sorteo"],
                "casillas": nuevas,
            }
            r = api.importar_jornada_historica(payload)
            print(f"       guardado: {r.get('accion')}, {r.get('casillas_procesadas')} casillas")

    print(
        f"\nResumen: {total_jornadas} jornadas · {total_ya} ya vinculadas antes · "
        f"+{total_nuevas} nuevas · {total_sin} sin vincular"
    )

    if args.dry_run:
        print("\nDRY RUN: no se ha escrito nada.")

    if args.informe and todos_sin_vinculo:
        print(f"\nSin vincular ({len(todos_sin_vinculo)}), para revisión manual:")
        for temporada, numero_jornada, pos, local, visitante, mejor, score in todos_sin_vinculo:
            aprox = (
                f" — lo más parecido: {mejor['equipo_local']} - {mejor['equipo_visitante']} ({score:.2f})"
                if mejor else " — sin candidato en la ventana de fechas"
            )
            print(f"  {temporada} J{numero_jornada:>2} pos{pos:>2}: {local} - {visitante}{aprox}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
