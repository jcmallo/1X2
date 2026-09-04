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
from datetime import datetime, timedelta, timezone

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



# El boleto se lee de eduardolosilla.es y no de SELAE: la página de juego de
# SELAE está geobloqueada y devuelve 403 desde fuera de España, que es donde
# corren los runners de GitHub Actions. Esta no lo está y sirve los nombres
# ya en el HTML, sin necesidad de JavaScript.
import requests as requests_normal


URL_BOLETO = "https://www.eduardolosilla.es/quiniela"
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
    Nombres de los 15 partidos y número de jornada.

    Se leen de eduardolosilla.es, que los sirve en el HTML sin JavaScript y
    sin geobloqueo. Los porcentajes de esa misma página NO se usan: los pinta
    Angular después de cargar, así que no están en el HTML, y además los
    oficiales se obtienen directamente de SELAE.

    Formato en la página: una tabla donde cada fila es

        1  ATH.CLUB - AT.MADRID   SAB 16:15

    y la casilla 15 (el Pleno) trae los dos equipos por separado.
    """
    r = requests_normal.get(
        URL_BOLETO, headers={"User-Agent": UA_NAVEGADOR}, timeout=30
    )
    r.raise_for_status()
    html = r.text

    texto = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    texto = re.sub(r"<style.*?</style>", " ", texto, flags=re.S | re.I)
    texto = re.sub(r"<[^>]+>", "\n", texto)
    lineas = [" ".join(x.split()) for x in texto.split("\n")]
    lineas = [x for x in lineas if x]

    nombres = {}
    for i, linea in enumerate(lineas):
        # Las filas del boleto son "N" seguido del enfrentamiento, o bien
        # "N NOMBRE - NOMBRE" en la misma línea.
        m = re.match(r"^(\d{1,2})\s+(.+?\s+-\s+.+)$", linea)
        if m:
            pos = int(m.group(1))
            partes = re.split(r"\s+-\s+", m.group(2), maxsplit=1)
            if 1 <= pos <= 15 and len(partes) == 2:
                nombres.setdefault(pos, (partes[0].strip(), partes[1].strip()))
            continue

        if re.fullmatch(r"\d{1,2}", linea):
            pos = int(linea)
            if not (1 <= pos <= 15) or pos in nombres:
                continue
            siguiente = lineas[i + 1] if i + 1 < len(lineas) else ""
            partes = re.split(r"\s+-\s+", siguiente, maxsplit=1)
            if len(partes) == 2:
                nombres[pos] = (partes[0].strip(), partes[1].strip())
            elif i + 2 < len(lineas) and re.match(r"^[A-ZÁÉÍÓÚÑ]", siguiente):
                # El Pleno al 15 lista los equipos en líneas separadas.
                nombres[pos] = (siguiente.strip(), lineas[i + 2].strip())

    # Horarios: tras cada partido vienen el día y la hora en líneas sueltas.
    horarios = []
    for i, linea in enumerate(lineas):
        if re.fullmatch(r"(LUN|MAR|MIE|MIÉ|JUE|VIE|SAB|SÁB|DOM)", linea, re.I):
            siguiente = lineas[i + 1] if i + 1 < len(lineas) else ""
            if re.fullmatch(r"\d{1,2}:\d{2}", siguiente):
                horarios.append((linea, siguiente))

    m_jornada = re.search(r"JORNADA\s*(\d+)", html, re.I)

    if len(nombres) < 14:
        raise RuntimeError(
            f"Solo se leyeron {len(nombres)} partidos del boleto en "
            f"{URL_BOLETO}. ¿Ha cambiado la estructura de la página?"
        )

    return {
        "nombres": nombres,
        "jornada": int(m_jornada.group(1)) if m_jornada else None,
        "horarios": horarios,
        "cierre": None,  # esta fuente no publica la hora exacta de cierre
    }


def leer_porcentajes(jornada: int, temporada: int) -> tuple[dict, dict | None, str]:
    """
    Porcentajes apostados. Devuelve las ternas, el Pleno y la huella usada.

    La respuesta viene indexada por posición y cada entrada trae 'orden',
    'valor1', 'valorx', 'valor2'. La posición 15 es el Pleno y llega con más
    campos, uno por cada resultado de goles de cada equipo.
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
            pleno = None
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

            # El Pleno viene en la posición 15 con los goles de cada equipo.
            pleno = _extraer_pleno(datos)

            if salida:
                return salida, pleno, huella
            ultimo_error = f"respuesta sin ternas válidas con {huella}"

        except Exception as exc:  # noqa: BLE001
            ultimo_error = f"{huella}: {exc}"

    raise RuntimeError(
        f"No se pudieron leer los porcentajes de SELAE. Último intento: {ultimo_error}"
    )


DIAS_SEMANA = {
    "LUN": 0, "MAR": 1, "MIE": 2, "MIÉ": 2,
    "JUE": 3, "VIE": 4, "SAB": 5, "SÁB": 5, "DOM": 6,
}


def primer_partido(horarios: list[tuple[str, str]], ahora: datetime) -> datetime | None:
    """
    Momento del primer partido de la jornada, a partir de sus horarios.

    El boleto trae el día de la semana y la hora ("SAB", "14:00") pero no la
    fecha. Se resuelve buscando la próxima ocurrencia de ese día a partir de
    hoy, que para una jornada en curso es siempre la correcta.

    Sirve como cota superior del cierre de ventas: la quiniela siempre cierra
    antes de que empiece el primer partido. No es la hora exacta de cierre,
    pero basta para clasificar la captura en su franja sin inventar un dato.
    """
    candidatos = []

    for dia, hora in horarios:
        indice = DIAS_SEMANA.get(dia.upper())
        if indice is None:
            continue
        try:
            hh, mm = (int(x) for x in hora.split(":"))
        except ValueError:
            continue

        adelanto = (indice - ahora.weekday()) % 7
        fecha = (ahora + timedelta(days=adelanto)).replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        # Si ese día es hoy pero la hora ya pasó, es el de la semana que viene.
        if fecha < ahora:
            fecha += timedelta(days=7)
        candidatos.append(fecha)

    return min(candidatos) if candidatos else None


# Cómo nombra SELAE los ocho valores del Pleno. El sufijo indica el lado:
# 'l' local, 'v' visitante. Comprobado sobre la respuesta real:
#
#   "14": {"orden":"15", "valor1":null, "valorx":null, "valor2":null,
#          "valor0l":"10","valor1l":"54","valor2l":"31","valorml":"5",
#          "valor0v":"19","valor1v":"52","valor2v":"23","valormv":"6"}
#
# Las ternas 1/X/2 llegan a null en esta posición, que es lo correcto: el
# Pleno no se juega a 1/X/2 sino a goles.
CLAVES_PLENO = {
    "local": ("valor0l", "valor1l", "valor2l", "valorml"),
    "visitante": ("valor0v", "valor1v", "valor2v", "valormv"),
}


def _extraer_pleno(datos: dict) -> dict | None:
    """
    Saca el Pleno al 15 de la respuesta de SELAE.

    Se leen las claves por nombre y no por posición. Una versión anterior las
    ordenaba alfabéticamente y tomaba las cuatro primeras como locales, pero
    ese orden intercala los dos lados (valor0l, valor0v, valor1l...), así que
    mezclaba equipos. La suma lo detectó y descartó el dato.

    Si algo no cuadra se devuelve None en lugar de guardar algo dudoso: el
    Pleno es la categoría de premio más alta y un dato mal leído ahí sale caro.
    """
    for clave, valor in datos.items():
        if not clave.isdigit() or not isinstance(valor, dict):
            continue
        if str(valor.get("orden")) != "15":
            continue

        lados = {}
        for lado, claves in CLAVES_PLENO.items():
            try:
                numeros = [float(valor[k]) for k in claves]
            except (KeyError, TypeError, ValueError):
                return None
            # Los cuatro resultados son excluyentes y cubren todos los casos,
            # así que tienen que sumar 100.
            if abs(sum(numeros) - 100) > 2:
                return None
            lados[lado] = numeros

        return {
            f"lae_{lado}": {
                "p0": round(n[0] / 100, 6), "p1": round(n[1] / 100, 6),
                "p2": round(n[2] / 100, 6), "pm": round(n[3] / 100, 6),
            }
            for lado, n in lados.items()
        }
    return None


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

    # El cierre no viene con el boleto: se aproxima con el primer partido de
    # la jornada, que ya está en nuestra base de datos.
    cierre = boleto["cierre"] or primer_partido(boleto.get("horarios", []), ahora)
    franja = franja_temporal(cierre, ahora)
    temporada = temporada_actual(ahora)
    anio_api = int(temporada.split("-")[0])

    print(f"  jornada {jornada} · temporada {temporada}")
    origen_cierre = "SELAE" if boleto["cierre"] else "primer partido"
    print(f"  cierre ({origen_cierre}): {cierre}  ->  franja {franja}")
    print(f"  partidos: {len(boleto['nombres'])}")

    print("\nLeyendo porcentajes de SELAE...")
    porcentajes, pleno, huella = leer_porcentajes(jornada, anio_api)
    print(f"  huella que funcionó: {huella}")
    print(f"  posiciones con datos: {len(porcentajes)}")
    print(f"  Pleno al 15: {'leído' if pleno else 'no disponible'}")

    casillas = []
    for pos in sorted(boleto["nombres"]):
        if pos > 14:
            # La casilla 15 entra sin terna 1X2: sus porcentajes son de goles
            # y van aparte, en quiniela_pleno15.
            casillas.append({
                "posicion": pos,
                "equipo_local_impreso": boleto["nombres"][pos][0][:100],
                "equipo_visitante_impreso": boleto["nombres"][pos][1][:100],
            })
            continue
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
    print(f"  casillas con porcentaje: {con_datos}/14  (+ la 15 sin terna)")

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
        # La franja distingue una captura previa del dato definitivo: no es lo
        # mismo lo que juega la gente el jueves que al cierre.
        "tipo_lae": franja if franja != "DESCONOCIDA" else "ESTIMADO",
        "casillas": casillas,
    }
    if pleno:
        payload["pleno15"] = pleno
    if boleto["cierre"]:
        payload["fecha_sorteo"] = boleto["cierre"].strftime("%Y-%m-%d")

    r = api.importar_jornada_historica(payload)
    print(f"\nGuardado: {r.get('accion')}, jornada_id {r.get('jornada_id')}, "
          f"{r.get('probabilidades')} probabilidades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
