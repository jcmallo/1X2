"""
Captura de los porcentajes oficiales de La Quiniela (SELAE).

Qué son estos datos
-------------------

El porcentaje de apuestas que lleva cada signo. No es la probabilidad de que
ocurra: es lo que juega la gente. La diferencia entre ambas cosas es de donde
sale el valor, porque el premio lo reparte el público y no el mercado (ver
analisis/HALLAZGOS.md).

Por qué hace falta curl_cffi
----------------------------

El endpoint de SELAE está tras Akamai, que identifica al cliente por su
huella TLS antes de leer una sola cabecera. Comprobado: 403 desde curl, 403
desde el servidor de IONOS, 403 con Referer, Origin, cookies de sesión,
HTTP/2 y HTTP/1.1. Un Chrome real recibe 200 desde la misma máquina donde
curl recibe 403, así que no es la IP.

curl_cffi replica el handshake de Chrome. No arranca un navegador: es un
cliente HTTP con la huella cambiada. Verificado en GitHub Actions:

    requests normal       HTTP 403
    curl_cffi chrome124   HTTP 200

Dos fuentes, dos caminos
------------------------

    juegos.loteriasyapuestas.es   los 15 nombres del boleto   (curl normal)
    www.loteriasyapuestas.es      los porcentajes por posición (curl_cffi)

El primero no está protegido; el segundo sí. Se combinan por posición.

Importante sobre el momento de la captura
-----------------------------------------

Los porcentajes cambian mientras la gente apuesta: una captura del jueves no
es la del cierre. Por eso cada snapshot se guarda con su franja temporal
(T-72, T-24, T-2, CIERRE), igual que las cuotas de mercado. Usar el
porcentaje definitivo como si hubiera estado disponible antes del partido
sería mirar el futuro.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingestion"))
from api_client import ApiIngesta  # noqa: E402

try:
    from curl_cffi import requests as cr
except ImportError as exc:
    print(f"No se pudo importar curl_cffi: {exc}")
    print("Instalar con: pip install 'curl_cffi>=0.16'")
    print("Hace falta para replicar la huella TLS de Chrome; sin ella")
    print("Akamai rechaza la petición a SELAE con 403.")
    sys.exit(1)



URL_BOLETO = "https://juegos.loteriasyapuestas.es/jugar/la-quiniela/apuesta"
URL_ESTADISTICAS = "https://www.loteriasyapuestas.es/servicios/estadisticas"

# Orden de preferencia; si Akamai endurece el filtro, alguno seguirá pasando.
HUELLAS = ["chrome124", "chrome120", "chrome116", "safari17_0"]

UA_NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def franja_temporal(cierre: datetime | None, ahora: datetime) -> str:
    """
    Antelación de la captura respecto al cierre de ventas.

    Es lo que permite después respetar las reglas anti data-leakage: al
    entrenar hay que usar el porcentaje disponible en ese momento, no el
    definitivo.
    """
    if cierre is None:
        return "DESCONOCIDA"
    horas = (cierre - ahora).total_seconds() / 3600
    if horas > 72:
        return "T-72+"
    if horas > 48:
        return "T-72"
    if horas > 12:
        return "T-24"
    if horas > 2:
        return "T-2"
    if horas > 0:
        return "CIERRE"
    return "POST"


def leer_boleto() -> dict:
    """
    Nombres de los 15 partidos, número de jornada y cierre de ventas.

    Se pide con curl_cffi igual que el endpoint de porcentajes. Desde una
    petición normal fuera de España, SELAE redirige a
    /es/geo/informacion-geobloqueo y devuelve 403: la página de juego está
    geobloqueada, aunque el endpoint de datos no lo esté.
    """
    ultimo = None
    html = None

    for huella in HUELLAS:
        try:
            r = cr.get(URL_BOLETO, impersonate=huella, timeout=30)
            if r.status_code == 200 and "geobloqueo" not in r.url:
                html = r.text
                break
            ultimo = (
                f"{huella}: HTTP {r.status_code}"
                + (" (geobloqueo)" if "geobloqueo" in r.url else "")
            )
        except Exception as exc:  # noqa: BLE001
            ultimo = f"{huella}: {exc}"

    if html is None:
        raise RuntimeError(
            "No se pudo leer el boleto de SELAE. "
            f"Último intento: {ultimo}. "
            "Si aparece 'geobloqueo', la página de juego solo es accesible "
            "desde España y hará falta otra vía para los nombres."
        )

    partidos = re.findall(
        r'numero-partido-completos"?>\s*(\d+)\.\s*</strong>\s*'
        r'<span class="nombre-partido-completo">\s*(.*?)\s*</span>',
        html,
        re.S,
    )
    nombres = {}
    for numero, texto in partidos:
        limpio = " ".join(texto.split())
        partes = re.split(r"\s+-\s+", limpio, maxsplit=1)
        if len(partes) == 2:
            nombres[int(numero)] = (partes[0].strip(), partes[1].strip())

    texto_plano = re.sub(r"<[^>]+>", " ", html)
    texto_plano = " ".join(texto_plano.split())

    m_jornada = re.search(r"Jornada\s*(\d+)", texto_plano)
    m_cierre = re.search(
        r"Cierre de ventas:\s*(\d{2}/\d{2}/\d{4})\s*-?\s*(\d{1,2})[:.](\d{2})",
        texto_plano,
    )

    cierre = None
    if m_cierre:
        try:
            cierre = datetime.strptime(
                f"{m_cierre.group(1)} {m_cierre.group(2)}:{m_cierre.group(3)}",
                "%d/%m/%Y %H:%M",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            cierre = None

    return {
        "nombres": nombres,
        "jornada": int(m_jornada.group(1)) if m_jornada else None,
        "cierre": cierre,
    }


def leer_porcentajes(jornada: int, temporada: int) -> tuple[dict, str]:
    """
    Porcentajes apostados por posición. Devuelve también qué huella funcionó.

    La respuesta viene indexada por posición como cadena ("0", "1", ...) y
    cada entrada trae 'orden', 'valor1', 'valorx', 'valor2'. La posición 15
    (el Pleno) tiene otro formato y se descarta aquí: se trata aparte.
    """
    ultimo_error = None

    for huella in HUELLAS:
        try:
            r = cr.get(
                URL_ESTADISTICAS,
                params={"jornada": jornada, "temporada": temporada},
                impersonate=huella,
                timeout=30,
            )
            if r.status_code != 200:
                ultimo_error = f"HTTP {r.status_code} con {huella}"
                continue

            datos = r.json()
            salida = {}
            for clave, valor in datos.items():
                if not clave.isdigit() or not isinstance(valor, dict):
                    continue
                orden = valor.get("orden")
                v1, vx, v2 = (
                    valor.get("valor1"),
                    valor.get("valorx"),
                    valor.get("valor2"),
                )
                if orden is None or None in (v1, vx, v2):
                    continue
                try:
                    pos = int(orden)
                    terna = (float(v1), float(vx), float(v2))
                except (TypeError, ValueError):
                    continue
                # Las ternas deben sumar 100; si no, algo se ha leído mal.
                if abs(sum(terna) - 100) > 2:
                    continue
                salida[pos] = terna

            if salida:
                return salida, huella
            ultimo_error = f"respuesta sin ternas válidas con {huella}"

        except Exception as exc:  # noqa: BLE001
            ultimo_error = f"{huella}: {exc}"

    raise RuntimeError(
        f"No se pudieron leer los porcentajes de SELAE. Último intento: {ultimo_error}"
    )


def temporada_actual(hoy: datetime | None = None) -> str:
    """La temporada de fútbol empieza en verano: 2026-27 desde julio de 2026."""
    hoy = hoy or datetime.now(timezone.utc)
    inicio = hoy.year if hoy.month >= 7 else hoy.year - 1
    return f"{inicio}-{str(inicio + 1)[-2:]}"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Captura los porcentajes oficiales de La Quiniela."
    )
    p.add_argument("--dry-run", action="store_true", help="no escribe, solo informa")
    p.add_argument("--jornada", type=int, default=0, help="forzar número de jornada")
    args = p.parse_args()

    print("Leyendo el boleto...")
    boleto = leer_boleto()
    jornada = args.jornada or boleto["jornada"]

    if not jornada:
        print("No se pudo determinar la jornada.")
        return 1
    if len(boleto["nombres"]) < 14:
        print(f"Solo {len(boleto['nombres'])} nombres de partido; se esperaban 15.")
        return 1

    ahora = datetime.now(timezone.utc)
    franja = franja_temporal(boleto["cierre"], ahora)
    temporada = temporada_actual(ahora)
    anio_api = int(temporada.split("-")[0])

    print(f"  jornada {jornada} · temporada {temporada}")
    print(f"  cierre: {boleto['cierre']}  ->  franja {franja}")
    print(f"  partidos: {len(boleto['nombres'])}")

    print("\nLeyendo porcentajes de SELAE...")
    porcentajes, huella = leer_porcentajes(jornada, anio_api)
    print(f"  huella que funcionó: {huella}")
    print(f"  posiciones con datos: {len(porcentajes)}")

    casillas = []
    for pos in sorted(boleto["nombres"]):
        if pos > 14:
            continue  # el Pleno al 15 tiene formato propio
        local, visitante = boleto["nombres"][pos]
        casilla = {
            "posicion": pos,
            "equipo_local_impreso": local[:100],
            "equipo_visitante_impreso": visitante[:100],
        }
        if pos in porcentajes:
            p1, px, p2 = porcentajes[pos]
            casilla["prob_lae"] = {
                "p1": round(p1 / 100, 6),
                "px": round(px / 100, 6),
                "p2": round(p2 / 100, 6),
            }
        casillas.append(casilla)

    con_datos = sum(1 for c in casillas if "prob_lae" in c)
    print(f"  casillas con porcentaje: {con_datos}/{len(casillas)}")

    print()
    for c in casillas:
        pl = c.get("prob_lae")
        pct = (
            f"{pl['p1']*100:>5.1f} {pl['px']*100:>5.1f} {pl['p2']*100:>5.1f}"
            if pl
            else "   sin datos"
        )
        print(
            f"  {c['posicion']:>2}. {c['equipo_local_impreso']:<24} "
            f"- {c['equipo_visitante_impreso']:<24} {pct}"
        )

    if args.dry_run:
        print("\nDRY RUN: no se ha escrito nada.")
        return 0

    if con_datos < 14:
        print(f"\nSolo {con_datos} casillas con porcentaje; no se guarda nada.")
        return 1

    api = ApiIngesta()
    payload = {
        "numero_jornada": jornada,
        "etiqueta_temporada": temporada,
        "fuente": "selae",
        "fuente_fichero": f"selae_estadisticas_{franja}",
        "casillas": casillas,
    }
    if boleto["cierre"]:
        payload["fecha_sorteo"] = boleto["cierre"].strftime("%Y-%m-%d")

    r = api.importar_jornada_historica(payload)
    print(f"\nGuardado: {r.get('accion')}, jornada_id {r.get('jornada_id')}, "
          f"{r.get('probabilidades')} probabilidades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
