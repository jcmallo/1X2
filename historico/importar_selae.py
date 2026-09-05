"""
Importa jornadas de La Quiniela desde el buscador oficial de SELAE.

Qué resuelve
------------

Faltaban las jornadas 1, 2 y 3 de la temporada porque empezamos a capturar
en la 4: los porcentajes se leen en directo y lo que no se capturó a tiempo
no se puede recuperar por esa vía. Pero SELAE mantiene un buscador de
sorteos celebrados que devuelve, por jornada:

  - los 15 partidos con nombres, marcador, fecha y hora
  - la combinación ganadora
  - el escrutinio completo: recaudación, apuestas, acertantes y premios

Es la fuente oficial, así que sustituye con ventaja a lo que veníamos
usando: aquí los premios son los que pagó SELAE, no una estimación.

    https://www.loteriasyapuestas.es/servicios/buscadorSorteos

Los porcentajes apostados no vienen en este endpoint. Se piden aparte al de
estadísticas, que sí sirve jornadas pasadas.

Uso
---

    python -m historico.importar_selae --desde 2026-08-01 --dry-run
    python -m historico.importar_selae --desde 2026-08-01 --hasta 2026-09-10
    python -m historico.importar_selae --temporada-actual
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_client import ApiIngesta  # noqa: E402

try:
    from curl_cffi import requests as cr
except ImportError:  # pragma: no cover
    cr = None
    import requests as cr_plano


BUSCADOR = "https://www.loteriasyapuestas.es/servicios/buscadorSorteos"
ESTADISTICAS = "https://www.loteriasyapuestas.es/servicios/estadisticas"

# Akamai identifica al cliente por la huella TLS del saludo inicial, no por
# el User-Agent. curl_cffi reproduce la de Chrome; requests a secas recibe
# un 403 aunque mande las mismas cabeceras.
HUELLAS = ["chrome124", "chrome120", "chrome116", "safari17_0"]

# Cómo nombra SELAE los ocho valores del Pleno. Sufijo 'l' local, 'v'
# visitante. Igual que en mercado/lae_porcentajes.py.
CLAVES_PLENO = {
    "local": ("valor0l", "valor1l", "valor2l", "valorml"),
    "visitante": ("valor0v", "valor1v", "valor2v", "valormv"),
}

# Segundos de espera entre jornadas. Importar el histórico completo son unas
# 1.100 jornadas y dos peticiones por cada una; ir sin pausa es la forma más
# rápida de que SELAE empiece a devolver 403.
PAUSA = 0.4


def pedir(url: str, params: dict) -> object:
    """GET con huella de navegador, probando varias si alguna falla."""
    if cr is None:
        raise RuntimeError(
            "Falta curl_cffi. SELAE rechaza las peticiones sin huella TLS "
            "de navegador. Instálalo con: pip install curl_cffi"
        )

    ultimo = None
    for huella in HUELLAS:
        try:
            r = cr.get(url, params=params, impersonate=huella, timeout=40)
            if r.status_code == 200:
                return r.json()
            ultimo = f"HTTP {r.status_code} con {huella}"
        except Exception as exc:  # noqa: BLE001
            ultimo = f"{huella}: {exc}"
    raise RuntimeError(f"No se pudo leer {url}. Último intento: {ultimo}")


def etiqueta_temporada(bruta: str) -> str:
    """
    SELAE dice '2026-2027' y la base guarda '2026-27'.

    Mezclar los dos formatos crearía dos temporadas distintas para el mismo
    año y las jornadas quedarían repartidas entre ambas.
    """
    m = re.match(r"^(\d{4})-(\d{4})$", bruta.strip())
    if m:
        return f"{m.group(1)}-{m.group(2)[2:]}"
    return bruta.strip()


def limpiar_equipo(nombre: str) -> str:
    """SELAE marca la competición con '(m)' o '(f)'; el boleto usa mayúsculas."""
    return re.sub(r"\s*\((?:m|f)\)\s*$", "", nombre.strip(), flags=re.I)


def es_femenino(nombre: str) -> bool:
    return bool(re.search(r"\(f\)\s*$", nombre.strip(), flags=re.I))


def signos_de_combinacion(combinacion: str) -> list[str | None]:
    """
    '1 - 1 - 2 - ... - M2' a una lista de 15 signos.

    El decimoquinto es el Pleno y no es 1/X/2 sino goles de cada equipo
    ('M2', '11', '0M'), así que se devuelve tal cual para tratarlo aparte.
    """
    partes = [p.strip() for p in combinacion.split("-")]
    salida: list[str | None] = []
    for i, p in enumerate(partes[:15]):
        if i == 14:
            salida.append(p or None)
        else:
            salida.append(p if p in ("1", "X", "2") else None)
    while len(salida) < 15:
        salida.append(None)
    return salida


def porcentajes_lae(jornada: int, anio: int) -> tuple[dict, dict | None]:
    """Porcentajes apostados de una jornada pasada, y su Pleno."""
    try:
        datos = pedir(ESTADISTICAS, {"jornada": jornada, "temporada": anio})
    except RuntimeError:
        return {}, None

    if not isinstance(datos, dict):
        return {}, None

    ternas = {}
    pleno = None

    for clave, valor in datos.items():
        if not clave.isdigit() or not isinstance(valor, dict):
            continue
        orden = valor.get("orden")
        if orden is None:
            continue

        if str(orden) == "15":
            lados = {}
            ok = True
            for lado, claves in CLAVES_PLENO.items():
                try:
                    nums = [float(valor[k]) for k in claves]
                except (KeyError, TypeError, ValueError):
                    ok = False
                    break
                if abs(sum(nums) - 100) > 2:
                    ok = False
                    break
                lados[lado] = nums
            if ok:
                pleno = {
                    f"lae_{lado}": {
                        "p0": round(n[0] / 100, 6), "p1": round(n[1] / 100, 6),
                        "p2": round(n[2] / 100, 6), "pm": round(n[3] / 100, 6),
                    }
                    for lado, n in lados.items()
                }
            continue

        try:
            pos = int(orden)
            v1 = float(valor["valor1"])
            vx = float(valor["valorx"])
            v2 = float(valor["valor2"])
        except (KeyError, TypeError, ValueError):
            continue

        if abs(v1 + vx + v2 - 100) > 2:
            continue
        ternas[pos] = {
            "p1": round(v1 / 100, 6),
            "px": round(vx / 100, 6),
            "p2": round(v2 / 100, 6),
        }

    return ternas, pleno


def a_float(v) -> float | None:
    """SELAE manda los importes con coma decimal."""
    if v is None:
        return None
    try:
        return float(str(v).replace(".", "").replace(",", ".")) if "," in str(v) else float(v)
    except (TypeError, ValueError):
        return None


def escrutinio_de(sorteo: dict) -> dict:
    """
    Recaudación, apuestas, precio y la categoría de 14 aciertos.

    El precio de la apuesta NO se da por sabido: se deriva de recaudación
    entre apuestas. Ha cambiado con los años —0,50 EUR hasta mediados de los
    2010 y 0,75 después— y fijarlo en 0,75 falsearía el ROI de media década
    de histórico en un 50%.
    """
    cat14 = {}
    for e in sorteo.get("escrutinio", []):
        if e.get("categoria") == 2:      # 1ª categoría = 14 aciertos
            cat14 = e
            break

    recaudacion = a_float(sorteo.get("recaudacion"))
    apuestas = a_float(sorteo.get("apuestas"))

    # La recaudación llega en céntimos: 114.980.550 con 1,53 millones de
    # apuestas daría 75 EUR por apuesta, que no tiene sentido.
    if recaudacion and apuestas and recaudacion / apuestas > 10:
        recaudacion = recaudacion / 100

    precio = None
    if recaudacion and apuestas:
        crudo = recaudacion / apuestas
        # Se redondea al céntimo y se comprueba que salga un precio real: si
        # no, es que uno de los dos números no es lo que parece y vale más
        # dejarlo en nulo que meter un precio inventado.
        redondeado = round(crudo, 2)
        if 0.10 <= redondeado <= 5.00:
            precio = redondeado

    return {
        "recaudacion": recaudacion,
        "apuestas_validadas": int(apuestas) if apuestas else None,
        "acertantes_14": int(a_float(cat14.get("ganadores")) or 0) if cat14 else None,
        "premio_14": a_float(cat14.get("premio")) if cat14 else None,
        "precio_apuesta": precio,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Importa jornadas celebradas desde el buscador de SELAE."
    )
    p.add_argument("--desde", default="", help="AAAA-MM-DD")
    p.add_argument("--hasta", default="", help="AAAA-MM-DD (por defecto, hoy)")
    p.add_argument(
        "--temporada-actual", action="store_true",
        help="desde el 1 de julio de la temporada en curso",
    )
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    args = p.parse_args()

    hoy = date.today()

    if args.temporada_actual:
        inicio = date(hoy.year if hoy.month >= 7 else hoy.year - 1, 7, 1)
    elif args.desde:
        inicio = datetime.strptime(args.desde, "%Y-%m-%d").date()
    else:
        inicio = hoy - timedelta(days=60)

    fin = datetime.strptime(args.hasta, "%Y-%m-%d").date() if args.hasta else hoy

    print(f"Buscando sorteos celebrados entre {inicio} y {fin}...")

    sorteos = pedir(BUSCADOR, {
        "game_id": "LAQU",
        "celebrados": "true",
        "fechaInicioInclusiva": inicio.strftime("%Y%m%d"),
        "fechaFinInclusiva": fin.strftime("%Y%m%d"),
    })

    if not isinstance(sorteos, list) or not sorteos:
        print("SELAE no ha devuelto ningún sorteo en ese rango.")
        return 1

    print(f"  {len(sorteos)} sorteos")
    if len(sorteos) > 60:
        minutos = len(sorteos) * (PAUSA + 1.6) / 60
        print(
            f"  A este ritmo son unos {minutos:.0f} minutos. Si prefieres "
            "trocearlo, usa rangos de fechas de una temporada."
        )
    print()

    api = ApiIngesta()
    importadas = 0
    sin_jornada = 0
    sin_precio = []

    for indice, s in enumerate(sorted(sorteos, key=lambda x: x.get("fecha_sorteo", "")), 1):
        if indice > 1:
            time.sleep(PAUSA)
        temporada = etiqueta_temporada(str(s.get("temporada", "")))
        try:
            jornada = int(s.get("jornada"))
        except (TypeError, ValueError):
            # Los sorteos anteriores a 2008 no traen número de jornada ni
            # número de apuestas, así que no se pueden identificar ni
            # normalizar. Se cuentan y se informa al final.
            sin_jornada += 1
            continue

        partidos = s.get("partidos", [])
        if len(partidos) < 15:
            print(f"  jornada {jornada} ({temporada}): solo {len(partidos)} partidos, se salta")
            continue

        signos = signos_de_combinacion(str(s.get("combinacion", "")))
        anio = int(str(s.get("anyo") or inicio.year))
        ternas, pleno = porcentajes_lae(jornada, anio)

        casillas = []
        for pt in sorted(partidos, key=lambda x: int(x["posicion"]))[:15]:
            pos = int(pt["posicion"])
            marca = " (F)" if es_femenino(str(pt.get("local", ""))) else ""

            casilla = {
                "posicion": pos,
                "equipo_local_impreso": limpiar_equipo(str(pt.get("local", ""))) + marca,
                "equipo_visitante_impreso": limpiar_equipo(str(pt.get("visitante", ""))) + marca,
            }

            if pos < 15:
                if signos[pos - 1]:
                    casilla["signo_oficial"] = signos[pos - 1]
            elif signos[14]:
                # El resultado del Pleno son goles, no 1/X/2: va en su propio
                # campo. SELAE lo manda como '11', 'M2', '0M' o '1-1'.
                casilla["signo_pleno_oficial"] = signos[14]
            if pos in ternas:
                casilla["prob_lae"] = ternas[pos]

            casillas.append(casilla)

        payload = {
            "numero_jornada": jornada,
            "etiqueta_temporada": temporada,
            "fecha_sorteo": str(s.get("fecha_sorteo", ""))[:10] or None,
            "fuente": "selae",
            "fuente_fichero": f"buscadorSorteos {s.get('id_sorteo')}",
            "tipo_lae": "CIERRE",
            "casillas": casillas,
            "escrutinio": escrutinio_de(s),
        }
        if pleno:
            payload["pleno15"] = pleno

        esc = payload["escrutinio"]
        if esc["precio_apuesta"] is None:
            sin_precio.append(f"{temporada}/J{jornada}")
        print(
            f"  [{indice:>4}/{len(sorteos)}] "
            f"jornada {jornada:>2} · {temporada} · {payload['fecha_sorteo']}  "
            f"{len(casillas)} casillas, {sum(1 for c in casillas if 'signo_oficial' in c)} signos, "
            f"{len(ternas)} con % LAE, pleno {'sí' if pleno else 'no'}"
        )
        if esc.get("premio_14"):
            precio = esc["precio_apuesta"]
            print(
                f"      14 aciertos: {esc['acertantes_14']} acertantes, "
                f"{esc['premio_14']:,.2f} EUR cada uno".replace(",", ".")
                + (f" · apuesta a {precio:.2f} EUR" if precio else " · precio desconocido")
            )

        if args.dry_run:
            continue

        try:
            r = api.importar_jornada_historica(payload)
            print(f"      guardada: {r.get('accion')}, {r.get('casillas')} casillas")
            importadas += 1
        except Exception as exc:  # noqa: BLE001
            print(f"      ERROR al guardar: {exc}")

    if sin_jornada:
        print(
            f"\n{sin_jornada} sorteos sin número de jornada, omitidos. Son "
            "anteriores a 2008: SELAE no da ni la jornada ni el número de "
            "apuestas de esos años, así que no se pueden identificar ni usar "
            "para calcular rentabilidad."
        )
    if sin_precio:
        print(
            f"\n{len(sin_precio)} jornadas sin precio de apuesta derivable: "
            + ", ".join(sin_precio[:8]) + ("..." if len(sin_precio) > 8 else "")
        )

    if args.dry_run:
        print("\nDRY RUN: no se ha guardado nada.")
    else:
        print(f"\n{importadas} jornadas importadas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
