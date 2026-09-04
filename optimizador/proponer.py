"""
Calcula la propuesta de boleto de una jornada y la guarda.

Qué hace
--------

Lee la jornada de `contexto_dashboard.php`, decide qué marcar en cada casilla
y guarda el resultado con `guardar_boleto.php`. Es el paso que convierte lo
que hay en la base en algo jugable.

Las decisiones las toman `optimizador/cartera.py` (las 14 primeras) y
`optimizador/pleno.py` (la 15). Aquí solo se traduce entre el formato de la
API y el de esos módulos, y se informa de lo que sale.

Qué esperar de esto
-------------------

No es un sistema que gane dinero. En el backtest sobre 989 jornadas con
premios reales, ninguna estrategia tuvo ROI positivo con significancia
estadística; la mejor (jugar solo donde el valor supera 1,30) dio -14,4% con
un intervalo de confianza del 90% de [-40%, +17%]. Lo que este optimizador
hace es reducir la pérdida esperada frente a jugar al favorito (-44,8%) o
siguiendo al público (-69,2%), buscando signos infrajugados: el premio se
reparte entre acertantes, así que un signo que acierta poca gente paga más.

Ver analisis/HALLAZGOS.md para los números completos.

Uso
---

    python -m optimizador.proponer --dry-run
    python -m optimizador.proponer --columnas 16
    python -m optimizador.proponer --temporada 2026-27 --jornada 4
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_client import ApiIngesta  # noqa: E402

from optimizador.cartera import (  # noqa: E402
    SIGNOS,
    Casilla,
    construir_cartera,
    resumen_texto,
)
from optimizador.pleno import (  # noqa: E402
    CATEGORIAS,
    CasillaPleno,
    resolver_pleno,
    resumen as resumen_pleno,
)


# Fuentes de porcentajes del público, de más a menos fiable. LAE_CIERRE son
# los definitivos; LAE_ESTIMADO, una captura previa al cierre.
FUENTES_PUBLICO = ("LAE_CIERRE", "LAE_ESTIMADO")

PRECIO_COLUMNA = 0.75


def _terna(d: dict | None) -> dict[str, float] | None:
    """Pasa {'p1','px','p2'} de la API a {'1','X','2'}, normalizado a 1."""
    if not d:
        return None
    try:
        valores = {"1": float(d["p1"]), "X": float(d["px"]), "2": float(d["p2"])}
    except (KeyError, TypeError, ValueError):
        return None
    total = sum(valores.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in valores.items()}


def _cuatro(d: dict | None) -> dict[str, float] | None:
    """Lo mismo para el Pleno: {'p0','p1','p2','pm'} -> {'0','1','2','M'}."""
    if not d:
        return None
    try:
        valores = {
            "0": float(d["p0"]), "1": float(d["p1"]),
            "2": float(d["p2"]), "M": float(d["pm"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    total = sum(valores.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in valores.items()}


def _publico(casilla: dict) -> dict[str, float] | None:
    """Los porcentajes del público, prefiriendo los de cierre."""
    probs = casilla.get("probabilidades")
    if not isinstance(probs, dict):
        return None
    for fuente in FUENTES_PUBLICO:
        terna = _terna(probs.get(fuente))
        if terna:
            return terna
    return None


def construir_casillas(datos: dict) -> tuple[list[Casilla], CasillaPleno | None, list[int]]:
    """
    Traduce la respuesta de la API a los objetos del optimizador.

    Devuelve las casillas del 1 al 14, el Pleno si está, y las posiciones que
    hubo que descartar por no tener porcentajes del público. Sin ese dato no
    se puede calcular valor, que es lo único que aporta este sistema.
    """
    casillas: list[Casilla] = []
    pleno: CasillaPleno | None = None
    descartadas: list[int] = []

    for c in sorted(datos.get("casillas", []), key=lambda x: int(x["posicion"])):
        pos = int(c["posicion"])

        if pos == 15:
            p15 = datos.get("pleno15")
            if isinstance(p15, dict):
                for fuente in FUENTES_PUBLICO:
                    bloque = p15.get(fuente)
                    if not isinstance(bloque, dict):
                        continue
                    local = _cuatro(bloque.get("local"))
                    visitante = _cuatro(bloque.get("visitante"))
                    if local and visitante:
                        pleno = CasillaPleno(
                            local=c["local"],
                            visitante=c["visitante"],
                            p_lae_local=local,
                            p_lae_visitante=visitante,
                        )
                        break
            if pleno is None:
                descartadas.append(pos)
            continue

        lae = _publico(c)
        if not lae:
            descartadas.append(pos)
            continue

        mercado = _terna(c.get("mercado"))
        casillas.append(Casilla(
            posicion=pos,
            local=c["local"],
            visitante=c["visitante"],
            p_lae=lae,
            p_prob=mercado,
            fuente_prob="mercado" if mercado else "sin_datos",
        ))

    return casillas, pleno, descartadas


def main() -> int:
    p = argparse.ArgumentParser(description="Calcula y guarda la propuesta de boleto.")
    p.add_argument("--temporada", default="", help="p.ej. 2026-27")
    p.add_argument("--jornada", type=int, default=0)
    p.add_argument(
        "--columnas", type=int, default=8,
        help="tope de combinaciones del boleto (8 = 3 dobles)",
    )
    p.add_argument(
        "--probabilidad-minima", type=float, default=0.30,
        help="no marcar un signo por debajo de esta probabilidad",
    )
    p.add_argument(
        "--abrir-pleno", type=int, default=0,
        help="categorías extra por lado en el Pleno (cada una multiplica el coste)",
    )
    p.add_argument("--dry-run", action="store_true", help="no guarda, solo informa")
    args = p.parse_args()

    api = ApiIngesta()

    datos = api.contexto_dashboard(
        temporada=args.temporada or None,
        numero_jornada=args.jornada or None,
    )
    jornada = datos.get("jornada")
    if not jornada:
        print(datos.get("mensaje", "No hay ninguna jornada cargada."))
        return 1

    print(
        f"Jornada {jornada['numero']} · {jornada['temporada']}  "
        f"({datos['casillas_con_mercado']} casillas con mercado, "
        f"{datos['casillas_vinculadas']} vinculadas)\n"
    )

    casillas, pleno, descartadas = construir_casillas(datos)

    if descartadas:
        print(
            f"  Sin porcentajes del público, quedan fuera: "
            f"{', '.join(str(d) for d in descartadas)}\n"
        )

    if not casillas:
        print("No hay ninguna casilla con datos suficientes. No se propone nada.")
        return 1

    sin_mercado = [c.posicion for c in casillas if not c.tiene_probabilidad]
    if sin_mercado:
        print(
            f"  Sin cuotas de mercado ({len(sin_mercado)}): "
            f"{', '.join(str(s) for s in sin_mercado)}\n"
            "  En estas no se puede calcular valor y se sigue al público, que\n"
            "  es justo lo que el backtest señala como peor estrategia. Un\n"
            "  modelo propio para Liga F cubriría este hueco.\n"
        )

    cartera = construir_cartera(
        casillas,
        presupuesto_columnas=args.columnas,
        probabilidad_minima=args.probabilidad_minima,
    )

    print(resumen_texto(cartera, precio_columna=PRECIO_COLUMNA))

    combinaciones = cartera["columnas"]

    if pleno:
        resolver_pleno(pleno, aperturas=args.abrir_pleno)
        print("\n" + resumen_pleno(pleno))
        combinaciones *= pleno.combinaciones
    else:
        print("\n15. Sin datos del Pleno: no se propone.")

    coste = combinaciones * PRECIO_COLUMNA
    print(
        f"\nTotal: {combinaciones} combinaciones · {coste:.2f} EUR "
        f"a {PRECIO_COLUMNA:.2f} EUR/columna"
    )

    # --- Guardado -----------------------------------------------------------

    selecciones = [
        {
            "posicion": c.posicion,
            "signos": [s for s in SIGNOS if s in c.marcados],
        }
        for c in casillas
    ]

    if pleno:
        # El endpoint guarda una sola lista de signos por casilla. Para el
        # Pleno se envían las categorías marcadas del local: es lo que cabe
        # en el modelo actual sin forzarlo. El lado visitante queda en el
        # resumen impreso pero no en la base, que es una limitación conocida
        # y no un descuido.
        selecciones.append({
            "posicion": 15,
            "signos": [c for c in CATEGORIAS if c in pleno.marcados_local],
        })

    if args.dry_run:
        print("\nDRY RUN: no se ha guardado nada.")
        return 0

    r = api.guardar_boleto({
        "numero_jornada": int(jornada["numero"]),
        "etiqueta_temporada": jornada["temporada"],
        "objetivo": "valor_esperado",
        "presupuesto_eur": round(coste, 2),
        "selecciones": selecciones,
    })
    print(
        f"\nGuardado: ejecución {r.get('ejecucion_id')} · "
        f"{r.get('selecciones')} casillas · {r.get('combinaciones')} combinaciones"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
