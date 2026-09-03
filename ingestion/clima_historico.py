"""
Backfill de clima por PARTIDO ya finalizado.

Para cada partido con estadio + coordenadas:
1) Condición meteorológica alrededor del saque inicial:
   Open-Meteo Historical Weather API.
   -> clima_observaciones
2) Lo que se pronosticaba 24 h antes:
   Open-Meteo Previous Runs API, *_previous_day1.
   -> clima_previsiones (horas_antelacion=24)
3) Lo que se pronosticaba 72 h antes:
   Open-Meteo Previous Runs API, *_previous_day3.
   -> clima_previsiones (horas_antelacion=72)

La previsión histórica es point-in-time: no utiliza el tiempo realmente
observado como si hubiera sido conocido antes del partido.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from api_client import ApiIngesta


ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

ZONA_PARTIDOS = ZoneInfo("Europe/Madrid")

HEADERS = {
    "User-Agent": "quiniela-1x2/1.0 https://1x2.juancarlosmallo.com",
    "Accept": "application/json",
}

ARCHIVE_HOURLY = (
    "temperature_2m,rain,relative_humidity_2m,wind_speed_10m"
)

BASE_PREV = (
    "temperature_2m",
    "apparent_temperature",
    "rain",
    "snowfall",
    "relative_humidity_2m",
    "wind_speed_10m",
    "surface_pressure",
    "cloud_cover",
)

# El Previous Runs API no expone todas las variables del Forecast API
# para cada offset. No pedimos probabilidad de lluvia, rachas o visibilidad.
PREVIOUS_HOURLY = ",".join(
    f"{variable}_previous_day{dia}"
    for dia in (1, 3)
    for variable in BASE_PREV
)

session = requests.Session()
session.headers.update(HEADERS)


def utc_naive(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def fecha_local_partido(valor: str) -> datetime:
    dt = datetime.fromisoformat(valor)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZONA_PARTIDOS)
    else:
        dt = dt.astimezone(ZONA_PARTIDOS)
    return dt


def request_json(
    url: str,
    *,
    params: dict,
    intentos: int = 3,
) -> tuple[str, dict, str, str]:
    ultimo_error = None

    for intento in range(1, intentos + 1):
        solicitado = utc_naive(datetime.now(timezone.utc))
        try:
            r = session.get(
                url,
                params=params,
                timeout=(10, 45),
            )
            respondido = utc_naive(datetime.now(timezone.utc))

            if r.status_code == 200:
                data = r.json()
                if not isinstance(data, dict):
                    raise RuntimeError("Respuesta JSON con formato inesperado.")
                return r.text, data, solicitado, respondido

            if r.status_code in {429, 500, 502, 503, 504}:
                ultimo_error = RuntimeError(
                    f"HTTP {r.status_code}: {r.text[:300]}"
                )
                if intento < intentos:
                    espera = 2 ** (intento - 1)
                    print(
                        f"    Open-Meteo HTTP {r.status_code}; "
                        f"reintento en {espera}s..."
                    )
                    time.sleep(espera)
                    continue

            r.raise_for_status()

        except (
            requests.Timeout,
            requests.ConnectionError,
            requests.JSONDecodeError,
        ) as exc:
            ultimo_error = exc
            if intento < intentos:
                espera = 2 ** (intento - 1)
                print(
                    f"    Open-Meteo temporalmente no disponible; "
                    f"reintento en {espera}s..."
                )
                time.sleep(espera)
                continue

    raise RuntimeError(
        f"Open-Meteo no respondió correctamente tras {intentos} intentos: "
        f"{ultimo_error}"
    )


def parse_hora_local(valor: str) -> datetime:
    dt = datetime.fromisoformat(valor)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZONA_PARTIDOS)
    return dt.astimezone(ZONA_PARTIDOS)


def indice_hora_mas_cercana(hourly: dict, objetivo_local: datetime) -> int:
    horas = hourly.get("time")
    if not isinstance(horas, list) or not horas:
        raise RuntimeError("La respuesta no contiene hourly.time.")

    mejor_i = None
    mejor_diff = None

    for i, valor in enumerate(horas):
        if not valor:
            continue
        dt = parse_hora_local(str(valor))
        diff = abs((dt - objetivo_local).total_seconds())

        if mejor_diff is None or diff < mejor_diff:
            mejor_i = i
            mejor_diff = diff

    if mejor_i is None or mejor_diff is None:
        raise RuntimeError("No hay horas válidas en la respuesta.")

    # Resolución horaria: un saque inicial :30 queda a 30 minutos.
    if mejor_diff > 90 * 60:
        raise RuntimeError(
            f"No hay dato meteorológico suficientemente cercano "
            f"al saque inicial (diferencia {mejor_diff / 60:.0f} min)."
        )

    return mejor_i


def valor(hourly: dict, clave: str, idx: int):
    serie = hourly.get(clave)
    if not isinstance(serie, list) or idx >= len(serie):
        return None
    return serie[idx]


def parametros_fecha(item: dict) -> tuple[datetime, str, str]:
    inicio_local = fecha_local_partido(str(item["fecha_hora_inicio"]))
    start_date = inicio_local.date().isoformat()
    # Incluimos el día siguiente para partidos cercanos a medianoche.
    end_date = (inicio_local.date() + timedelta(days=1)).isoformat()
    return inicio_local, start_date, end_date


def cargar_observacion(
    api: ApiIngesta,
    lote_id: int,
    item: dict,
) -> None:
    inicio_local, start_date, end_date = parametros_fecha(item)

    params = {
        "latitude": round(float(item["latitud"]), 5),
        "longitude": round(float(item["longitud"]), 5),
        "start_date": start_date,
        "end_date": end_date,
        "hourly": ARCHIVE_HOURLY,
        "timezone": "Europe/Madrid",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }

    raw, data, solicitado, respondido = request_json(
        ARCHIVE_URL,
        params=params,
    )

    hourly = data.get("hourly")
    if not isinstance(hourly, dict):
        raise RuntimeError("Historical Weather API sin hourly válido.")

    idx = indice_hora_mas_cercana(hourly, inicio_local)
    hora_dato_local = parse_hora_local(str(hourly["time"][idx]))

    payload = {
        "tipo": "observacion",
        "lote_id": lote_id,
        "partido_id": int(item["partido_id"]),
        "estadio_id": int(item["estadio_id"]),
        "observado_en": utc_naive(hora_dato_local),
        "temperatura_c": valor(hourly, "temperature_2m", idx),
        "lluvia_mm": valor(hourly, "rain", idx),
        "humedad_pct": valor(hourly, "relative_humidity_2m", idx),
        "velocidad_viento_kmh": valor(hourly, "wind_speed_10m", idx),
        "fuente": "open-meteo-historical",
        "endpoint": ARCHIVE_URL,
        "parametros_solicitud": params,
        "solicitado_en": solicitado,
        "respondido_en": respondido,
        "codigo_http": 200,
        "raw_payload": raw,
    }

    res = api.guardar_clima_historico(payload)
    print(
        f"    OBS {res.get('accion')} id={res.get('registro_id')}"
    )


def cargar_previsiones(
    api: ApiIngesta,
    lote_id: int,
    item: dict,
    leads: list[int],
) -> None:
    inicio_local, start_date, end_date = parametros_fecha(item)

    dias_necesarios = sorted({1 if h == 24 else 3 for h in leads})
    variables = ",".join(
        f"{variable}_previous_day{dia}"
        for dia in dias_necesarios
        for variable in BASE_PREV
    )

    params = {
        "latitude": round(float(item["latitud"]), 5),
        "longitude": round(float(item["longitud"]), 5),
        "start_date": start_date,
        "end_date": end_date,
        "hourly": variables,
        "timezone": "Europe/Madrid",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }

    raw, data, solicitado, respondido = request_json(
        PREVIOUS_RUNS_URL,
        params=params,
    )

    hourly = data.get("hourly")
    if not isinstance(hourly, dict):
        raise RuntimeError("Previous Runs API sin hourly válido.")

    idx = indice_hora_mas_cercana(hourly, inicio_local)
    hora_dato_local = parse_hora_local(str(hourly["time"][idx]))

    for horas in leads:
        dia = 1 if horas == 24 else 3
        sufijo = f"_previous_day{dia}"

        temp = valor(hourly, f"temperature_2m{sufijo}", idx)

        # Sin temperatura no consideramos que exista una previsión útil.
        if temp is None:
            raise RuntimeError(
                f"Previous Runs no devolvió temperatura para T-{horas}h."
            )

        nieve_cm = valor(hourly, f"snowfall{sufijo}", idx)
        nieve_mm = (
            float(nieve_cm) * 10.0
            if nieve_cm is not None
            else None
        )

        prevista_utc = hora_dato_local.astimezone(timezone.utc)
        generada_utc = prevista_utc - timedelta(hours=horas)

        payload = {
            "tipo": "prevision",
            "lote_id": lote_id,
            "partido_id": int(item["partido_id"]),
            "estadio_id": int(item["estadio_id"]),
            "prevision_generada_en": utc_naive(generada_utc),
            "prevista_para": utc_naive(prevista_utc),
            "horas_antelacion": horas,
            "temperatura_c": temp,
            "sensacion_termica_c": valor(
                hourly, f"apparent_temperature{sufijo}", idx
            ),
            "lluvia_mm": valor(hourly, f"rain{sufijo}", idx),
            # Previous Runs no expone esta variable para los offsets usados.
            "probabilidad_lluvia_pct": None,
            "humedad_pct": valor(
                hourly, f"relative_humidity_2m{sufijo}", idx
            ),
            "velocidad_viento_kmh": valor(
                hourly, f"wind_speed_10m{sufijo}", idx
            ),
            "rachas_viento_kmh": None,
            "presion_hpa": valor(
                hourly, f"surface_pressure{sufijo}", idx
            ),
            "nubosidad_pct": valor(
                hourly, f"cloud_cover{sufijo}", idx
            ),
            "visibilidad_km": None,
            "nieve_mm": nieve_mm,
            "fuente": "open-meteo-previous-runs",
            "endpoint": PREVIOUS_RUNS_URL,
            "parametros_solicitud": params,
            "solicitado_en": solicitado,
            "respondido_en": respondido,
            "codigo_http": 200,
            "raw_payload": raw,
        }

        res = api.guardar_clima_historico(payload)
        print(
            f"    PREV T-{horas}h {res.get('accion')} "
            f"id={res.get('registro_id')}"
        )


def main() -> None:
    limite = int(os.environ.get("CLIMA_HISTORICO_LIMITE", "500"))

    api = ApiIngesta()

    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    items = api.contexto_clima_historico(limite=limite)

    print("Partidos con clima histórico pendiente:", len(items))

    if not items:
        print("No hay trabajo pendiente.")
        return

    lote_id = api.iniciar_lote(
        fuente="open-meteo-historical-backfill",
        tipo_fuente="api",
        notas=(
            "Backfill clima por partido: observación + "
            "previsión T-24h/T-72h."
        ),
    )
    print("Lote abierto:", lote_id)

    completos = 0
    errores: list[str] = []

    for item in items:
        nombre = (
            f"{item.get('local')} - {item.get('visitante')} "
            f"[{item.get('competicion')}]"
        )
        print(
            f"- partido_id={item.get('partido_id')} | "
            f"{item.get('fecha_hora_inicio')} | {nombre}"
        )

        try:
            if int(item.get("falta_observacion", 0)) == 1:
                cargar_observacion(api, lote_id, item)

            leads = []
            if int(item.get("falta_prev24", 0)) == 1:
                leads.append(24)
            if int(item.get("falta_prev72", 0)) == 1:
                leads.append(72)

            if leads:
                cargar_previsiones(api, lote_id, item, leads)

            completos += 1

        except Exception as exc:
            errores.append(
                f"partido_id={item.get('partido_id')} {nombre}: {exc}"
            )
            print("    ERROR:", exc)

        # Ritmo prudente entre partidos.
        time.sleep(0.15)

    if errores and completos:
        estado = "parcial"
    elif errores:
        estado = "error"
    else:
        estado = "completado"

    notas = (
        f"partidos_ok={completos}; "
        f"errores={len(errores)}; "
        f"total={len(items)}"
    )
    if errores:
        notas += " | " + " | ".join(errores[:3])

    api.finalizar_lote(
        lote_id,
        estado=estado,
        notas=notas,
    )

    print(
        f"\nResumen: partidos_ok={completos}, "
        f"errores={len(errores)}, total={len(items)}"
    )

    if errores:
        raise RuntimeError(
            f"Backfill terminó con {len(errores)} error(es). "
            f"Primer error: {errores[0]}"
        )


if __name__ == "__main__":
    main()
