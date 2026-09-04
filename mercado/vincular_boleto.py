"""
Vincula las casillas del boleto con los partidos de nucleo_partidos.

Por qué hace falta
------------------

El boleto publica los nombres como los imprime SELAE ("ATH.CLUB",
"R.SOCIEDAD B", "RACING S.") y el importador los guarda tal cual, con
`partido_id` a NULL. Sin ese vínculo no se pueden traer las cuotas de
mercado, que están indexadas por partido: el panel muestra la columna del
público pero no la del mercado.

Este script hace el emparejamiento y lo guarda. Reutiliza
`guardar_jornada_quiniela.php`, que acepta `partido_id` por casilla y usa
COALESCE, así que reenviar una casilla sin vínculo no borra el que ya tenga.

Cómo empareja
-------------

Por nombre normalizado y fecha, con los mismos alias que el capturador de
cuotas. Las abreviaturas del boleto no coinciden con `nucleo_equipos`:

    ATH.CLUB        ->  Athletic Club
    RACING S.       ->  Real Racing Club de Santander
    DEPORTIVO       ->  RC Deportivo
    R.SOCIEDAD B    ->  Real Sociedad B

No empareja a la fuerza: lo que no supere el umbral se deja sin vincular y
se informa. Un vínculo equivocado sería peor que ninguno, porque metería en
el panel las cuotas de otro partido.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
from api_client import ApiIngesta  # noqa: E402


# Palabras que no distinguen al club. 'real' no está: separa Real Madrid de
# Real Sociedad y de Real Betis.
RUIDO = {
    "fc", "cf", "cd", "rcd", "ud", "sd", "rc", "ca", "ce", "ad", "club",
    "de", "futbol", "balompie", "femenino", "femeni", "women", "united",
}

# Cómo abrevia el boleto frente a cómo nombra nucleo_equipos.
ALIAS = {
    "ath club": "athletic",
    "athletic bilbao": "athletic",
    "racing s": "racing",
    "racing santander": "racing",
    "r racing": "racing",
    "real racing santander": "racing",
    "deportivo": "deportivo coruna",
    "rc deportivo": "deportivo coruna",
    "la coruna": "deportivo coruna",
    "d coruna": "deportivo coruna",
    "celta vigo": "celta",
    "espanyol barcelona": "espanyol",
    "r sociedad b": "real sociedad b",
    "sociedad b": "real sociedad b",
    "r madrid": "real madrid",
    "at madrid": "atletico madrid",
    "atletico madrid": "atletico madrid",
    "sp gijon": "sporting gijon",
    "sporting": "sporting gijon",
}

UMBRAL = 0.72
VENTANA_DIAS = 8


def normalizar(nombre: str) -> str:
    # El boleto marca la competición con "(F)" o "(M)". Hay que quitarlo antes
    # de nada: si no, "ATH.CLUB (F)" queda como "ath club f" y deja de casar
    # con el alias "ath club" -> "athletic".
    nombre = re.sub(r"\((?:F|M)\)", " ", nombre, flags=re.I)

    txt = unicodedata.normalize("NFKD", nombre)
    txt = "".join(c for c in txt if not unicodedata.combining(c)).lower()
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)
    palabras = [p for p in txt.split() if p]
    filtradas = [p for p in palabras if p not in RUIDO]
    base = " ".join(filtradas) if filtradas else " ".join(palabras)
    return ALIAS.get(base, base)


def parecido(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def es_femenino(nombre: str) -> bool:
    """El boleto marca la Liga F con '(F)'."""
    return "(f)" in nombre.lower()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Vincula el boleto con los partidos de la base."
    )
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    p.add_argument("--temporada", default="", help="p.ej. 2026-27")
    p.add_argument("--jornada", type=int, default=0)
    args = p.parse_args()

    api = ApiIngesta()

    jornadas = api.contexto_quiniela(
        temporada=args.temporada or None,
        numero_jornada=args.jornada or None,
        limite=1,
    )
    if not jornadas:
        print("No hay ninguna jornada cargada con esos criterios.")
        return 1

    jornada = jornadas[0]
    casillas = jornada.get("casillas", [])
    print(
        f"Jornada {jornada['numero_jornada']} · {jornada['etiqueta_temporada']} "
        f"· {len(casillas)} casillas"
    )
    ya = sum(1 for c in casillas if c.get("partido_id"))
    print(f"  ya vinculadas: {ya}")

    partidos = api.contexto_cuotas(
        dias_futuro=VENTANA_DIAS,
        retro_horas=48,
        solo_sin_cuotas=False,
        limite=500,
    )
    print(f"  partidos candidatos en la base: {len(partidos)}\n")

    preparados = []
    for p_ in partidos:
        try:
            inicio = datetime.fromisoformat(p_["fecha_hora_inicio"]).replace(
                tzinfo=timezone(timedelta(hours=2))
            )
        except (KeyError, ValueError):
            continue
        preparados.append({
            "partido_id": int(p_["partido_id"]),
            "local": p_["equipo_local"],
            "visitante": p_["equipo_visitante"],
            "local_n": normalizar(p_["equipo_local"]),
            "visitante_n": normalizar(p_["equipo_visitante"]),
            "genero": p_.get("genero", ""),
            "inicio": inicio,
        })

    nuevas = []
    sin_vinculo = []

    for c in sorted(casillas, key=lambda x: x["posicion"]):
        pos = int(c["posicion"])
        local = c["equipo_local_impreso"]
        visitante = c["equipo_visitante_impreso"]

        entrada = {
            "posicion": pos,
            "equipo_local_impreso": local,
            "equipo_visitante_impreso": visitante,
        }

        if c.get("partido_id"):
            entrada["partido_id"] = int(c["partido_id"])
            nuevas.append(entrada)
            continue

        # El género del boleto acota mucho: evita cruzar Sevilla con Sevilla (F).
        femenino = es_femenino(local) or es_femenino(visitante)
        genero = "FEMENINO" if femenino else "MASCULINO"

        nl, nv = normalizar(local), normalizar(visitante)
        mejor, mejor_score = None, 0.0

        for cand in preparados:
            if cand["genero"] and cand["genero"] != genero:
                continue
            score = (
                parecido(nl, cand["local_n"]) + parecido(nv, cand["visitante_n"])
            ) / 2
            if score > mejor_score:
                mejor_score, mejor = score, cand

        if mejor and mejor_score >= UMBRAL:
            entrada["partido_id"] = mejor["partido_id"]
            nuevas.append(entrada)
            print(
                f"  {pos:>2}. [{mejor_score:.2f}] {local} - {visitante}"
                f"  ->  {mejor['local']} - {mejor['visitante']}"
            )
        else:
            nuevas.append(entrada)
            sin_vinculo.append((pos, local, visitante, mejor, mejor_score))

    if sin_vinculo:
        print(f"\n  Sin vincular ({len(sin_vinculo)}):")
        for pos, local, visitante, mejor, score in sin_vinculo:
            aprox = (
                f" — lo más parecido: {mejor['local']} - {mejor['visitante']} ({score:.2f})"
                if mejor else ""
            )
            print(f"  {pos:>2}. {local} - {visitante}{aprox}")

    vinculadas = sum(1 for e in nuevas if "partido_id" in e)
    print(f"\n  vinculadas ahora: {vinculadas}/{len(nuevas)}")

    if args.dry_run:
        print("\nDRY RUN: no se ha escrito nada.")
        return 0

    if vinculadas == ya:
        print("\nNo hay vínculos nuevos que guardar.")
        return 0

    payload = {
        "numero_jornada": int(jornada["numero_jornada"]),
        "etiqueta_temporada": jornada["etiqueta_temporada"],
        "fuente": jornada.get("fuente", "selae"),
        "casillas": nuevas,
    }
    if jornada.get("fecha_sorteo"):
        payload["fecha_sorteo"] = jornada["fecha_sorteo"]

    # Se usa importar_jornada_historica y no guardar_jornada_quiniela porque
    # este acepta la jornada sin fecha de sorteo. El otro la sigue exigiendo
    # aunque la columna ya admita NULL: una incoherencia entre dos endpoints
    # que escriben en la misma tabla, pendiente de arreglar.
    r = api.importar_jornada_historica(payload)
    print(f"\nGuardado: {r.get('accion')}, {r.get('casillas_procesadas')} casillas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
