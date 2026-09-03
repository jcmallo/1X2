"""
Ingestor de clima robusto para Quiniela 1X2.

Arquitectura:
    GitHub Actions -> proveedor meteorológico -> API PHP IONOS -> MariaDB

Estrategia:
1) Open-Meteo es el proveedor principal.
2) Reintenta automáticamente errores transitorios (429/5xx/timeouts).
3) Pide solo el horizonte realmente necesario para no cargar la API con 14 días
   cuando solo necesitamos T+24h.
4) Si Open-Meteo sigue fallando, usa MET Norway Locationforecast 2.0 como
   proveedor de respaldo, sin clave API.
5) Si un proveedor falla, el RAW guardado corresponde SIEMPRE al proveedor que
   realmente entregó los datos; no se inventan campos ausentes.

Variables de entorno:
    INGEST_API_URL
    INGEST_API_TOKEN
    CLIMA_MODO=estadios|proximos
    CLIMA_DIAS=7
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from api_client import ApiIngesta


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
ZONA_PARTIDOS = ZoneInfo("Europe/Madrid")

# Para la prueba inicial no necesitamos 14 días completos.
# En modo proximos se calcula dinámicamente según la fecha del partido.
MARGEN_HORAS = 12

# Variables suficientes para el modelo; las variables enriquecidas se piden
# cuando Open-Meteo está disponible.
OPEN_METEO_HOURLY = (
    "temperature_2m,apparent_temperature,precipitation,"
    "precipitation_probability,snowfall,wind_speed_10m,"
    "wind_gusts_10m,relative_humidity_2m,surface_pressure,"
    "cloud_cover,visibility"
)

HEADERS_OPEN_METEO = {
    "User-Agent": "quiniela-1x2/1.0 https://1x2.juancarlosmallo.com"
}
HEADERS_MET_NO = {
    # MET Norway exige identificación clara en User-Agent.
    "User-Agent": "quiniela-1x2/1.0 https://1x2.juancarlosmallo.com",
    "Accept": "application/json",
}


def utc_naive(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def interpretar_inicio_partido(fecha_hora_inicio: str) -> datetime:
    dt_local = datetime.fromisoformat(fecha_hora_inicio)
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=ZONA_PARTIDOS)
    return dt_local.astimezone(timezone.utc)


def objetivo_para_item(item: dict, ahora_utc: datetime, modo: str) -> datetime:
    if modo == "proximos" and item.get("fecha_hora_inicio"):
        return interpretar_inicio_partido(str(item["fecha_hora_inicio"]))
    return ahora_utc + timedelta(hours=24)


def forecast_days_necesarios(ahora_utc: datetime, objetivo_utc: datetime) -> int:
    horas = max(
        24,
        (objetivo_utc - ahora_utc).total_seconds() / 3600 + MARGEN_HORAS,
    )
    # Open-Meteo admite hasta 16; nuestro CLIMA_DIAS está limitado a 14.
    return max(2, min(16, int(math.ceil(horas / 24)) + 1))


def request_con_reintentos(
    url: str,
    *,
    params: dict,
    headers: dict,
    intentos: int = 3,
    timeout: tuple[int, int] = (10, 30),
) -> requests.Response:
    ultimo_error = None

    for intento in range(1, intentos + 1):
        try:
            r = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )

            if r.status_code == 200:
                return r

            if r.status_code in {429, 500, 502, 503, 504}:
                ultimo_error = RuntimeError(
                    f"HTTP {r.status_code}: {r.text[:300]}"
                )
                if intento < intentos:
                    espera = 2 ** (intento - 1)
                    print(
                        f"    proveedor respondió HTTP {r.status_code}; "
                        f"reintento {intento}/{intentos} en {espera}s..."
                    )
                    time.sleep(espera)
                    continue

            r.raise_for_status()

        except (requests.Timeout, requests.ConnectionError) as exc:
            ultimo_error = exc
            if intento < intentos:
                espera = 2 ** (intento - 1)
                print(
                    f"    error transitorio ({type(exc).__name__}); "
                    f"reintento {intento}/{intentos} en {espera}s..."
                )
                time.sleep(espera)
                continue
            break

    raise RuntimeError(
        f"Proveedor meteorológico no disponible tras {intentos} intentos: "
        f"{ultimo_error}"
    )


def pedir_open_meteo(
    latitud: float,
    longitud: float,
    ahora_utc: datetime,
    objetivo_utc: datetime,
) -> tuple[str, dict, dict]:
    parametros = {
        "latitude": round(float(latitud), 4),
        "longitude": round(float(longitud), 4),
        "hourly": OPEN_METEO_HOURLY,
        "forecast_days": forecast_days_necesarios(ahora_utc, objetivo_utc),
        "timezone": "UTC",
    }

    r = request_con_reintentos(
        OPEN_METEO_URL,
        params=parametros,
        headers=HEADERS_OPEN_METEO,
    )
    return r.text, r.json(), parametros


def punto_open_meteo_mas_cercano(hourly: dict, objetivo_utc: datetime) -> int:
    horas = hourly.get("time")
    if not horas:
        raise RuntimeError("Open-Meteo no devolvió hourly.time")

    mejor_i = 0
    mejor_diff = None
    for i, hora in enumerate(horas):
        dt = datetime.fromisoformat(hora).replace(tzinfo=timezone.utc)
        diff = abs((dt - objetivo_utc).total_seconds())
        if mejor_diff is None or diff < mejor_diff:
            mejor_diff = diff
            mejor_i = i
    return mejor_i


def val(lista_or_none, idx):
    if not isinstance(lista_or_none, list) or idx >= len(lista_or_none):
        return None
    return lista_or_none[idx]


def normalizar_open_meteo(
    *,
    lote_id: int,
    item: dict,
    ahora_utc: datetime,
    objetivo_utc: datetime,
    texto: str,
    datos: dict,
    parametros: dict,
) -> dict:
    h = datos.get("hourly")
    if not isinstance(h, dict):
        raise RuntimeError("Open-Meteo no devolvió hourly válido")

    idx = punto_open_meteo_mas_cercano(h, objetivo_utc)
    prevista_utc = datetime.fromisoformat(h["time"][idx]).replace(
        tzinfo=timezone.utc
    )
    horas_antelacion = max(
        0, int(round((prevista_utc - ahora_utc).total_seconds() / 3600))
    )

    vis_m = val(h.get("visibility"), idx)

    return {
        "lote_id": lote_id,
        "estadio_id": int(item["estadio_id"]),
        "partido_id": (
            int(item["partido_id"])
            if item.get("partido_id") not in (None, "")
            else None
        ),
        "prevision_generada_en": utc_naive(ahora_utc),
        "prevista_para": utc_naive(prevista_utc),
        "horas_antelacion": horas_antelacion,
        "temperatura_c": val(h.get("temperature_2m"), idx),
        "sensacion_termica_c": val(h.get("apparent_temperature"), idx),
        "lluvia_mm": val(h.get("precipitation"), idx),
        "probabilidad_lluvia_pct": val(
            h.get("precipitation_probability"), idx
        ),
        "humedad_pct": val(h.get("relative_humidity_2m"), idx),
        "velocidad_viento_kmh": val(h.get("wind_speed_10m"), idx),
        "rachas_viento_kmh": val(h.get("wind_gusts_10m"), idx),
        "presion_hpa": val(h.get("surface_pressure"), idx),
        "nubosidad_pct": val(h.get("cloud_cover"), idx),
        "visibilidad_km": (
            float(vis_m) / 1000.0 if vis_m is not None else None
        ),
        "nieve_mm": val(h.get("snowfall"), idx),
        "fuente": "open-meteo",
        "endpoint": OPEN_METEO_URL,
        "parametros_solicitud": parametros,
        "solicitado_en": utc_naive(ahora_utc),
        "respondido_en": utc_naive(datetime.now(timezone.utc)),
        "codigo_http": 200,
        "raw_payload": texto,
    }


def pedir_met_no(
    latitud: float,
    longitud: float,
) -> tuple[str, dict, dict]:
    # La documentación de MET Norway recomienda no usar más de 4 decimales.
    parametros = {
        "lat": round(float(latitud), 4),
        "lon": round(float(longitud), 4),
    }
    r = request_con_reintentos(
        MET_NO_URL,
        params=parametros,
        headers=HEADERS_MET_NO,
    )
    return r.text, r.json(), parametros


def punto_met_no_mas_cercano(timeseries: list, objetivo_utc: datetime) -> dict:
    if not timeseries:
        raise RuntimeError("MET Norway no devolvió timeseries")

    mejor = None
    mejor_diff = None
    for punto in timeseries:
        hora = punto.get("time")
        if not hora:
            continue
        dt = datetime.fromisoformat(hora.replace("Z", "+00:00"))
        diff = abs((dt - objetivo_utc).total_seconds())
        if mejor_diff is None or diff < mejor_diff:
            mejor_diff = diff
            mejor = punto

    if mejor is None:
        raise RuntimeError("MET Norway no devolvió horas válidas")
    return mejor


def normalizar_met_no(
    *,
    lote_id: int,
    item: dict,
    ahora_utc: datetime,
    objetivo_utc: datetime,
    texto: str,
    datos: dict,
    parametros: dict,
) -> dict:
    props = datos.get("properties", {})
    timeseries = props.get("timeseries", [])
    punto = punto_met_no_mas_cercano(timeseries, objetivo_utc)

    prevista_utc = datetime.fromisoformat(
        punto["time"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)

    instant = punto.get("data", {}).get("instant", {}).get("details", {})
    next_1h = punto.get("data", {}).get("next_1_hours", {}).get("details", {})

    horas_antelacion = max(
        0, int(round((prevista_utc - ahora_utc).total_seconds() / 3600))
    )

    # MET Norway compact no ofrece exactamente todas las variables del esquema.
    # Las que no tienen equivalente exacto se dejan NULL deliberadamente.
    return {
        "lote_id": lote_id,
        "estadio_id": int(item["estadio_id"]),
        "partido_id": (
            int(item["partido_id"])
            if item.get("partido_id") not in (None, "")
            else None
        ),
        "prevision_generada_en": utc_naive(ahora_utc),
        "prevista_para": utc_naive(prevista_utc),
        "horas_antelacion": horas_antelacion,
        "temperatura_c": instant.get("air_temperature"),
        "sensacion_termica_c": None,
        "lluvia_mm": next_1h.get("precipitation_amount"),
        "probabilidad_lluvia_pct": next_1h.get(
            "probability_of_precipitation"
        ),
        "humedad_pct": instant.get("relative_humidity"),
        # MET Norway entrega m/s; el esquema guarda km/h.
        "velocidad_viento_kmh": (
            float(instant["wind_speed"]) * 3.6
            if instant.get("wind_speed") is not None
            else None
        ),
        "rachas_viento_kmh": (
            float(instant["wind_speed_of_gust"]) * 3.6
            if instant.get("wind_speed_of_gust") is not None
            else None
        ),
        # air_pressure_at_sea_level NO equivale a surface_pressure.
        "presion_hpa": None,
        "nubosidad_pct": instant.get("cloud_area_fraction"),
        "visibilidad_km": None,
        "nieve_mm": None,
        "fuente": "met-no",
        "endpoint": MET_NO_URL,
        "parametros_solicitud": parametros,
        "solicitado_en": utc_naive(ahora_utc),
        "respondido_en": utc_naive(datetime.now(timezone.utc)),
        "codigo_http": 200,
        "raw_payload": texto,
    }


def obtener_payload_clima(
    *,
    lote_id: int,
    item: dict,
    ahora_utc: datetime,
    objetivo_utc: datetime,
) -> dict:
    lat = float(item["latitud"])
    lon = float(item["longitud"])

    try:
        texto, datos, parametros = pedir_open_meteo(
            lat, lon, ahora_utc, objetivo_utc
        )
        return normalizar_open_meteo(
            lote_id=lote_id,
            item=item,
            ahora_utc=ahora_utc,
            objetivo_utc=objetivo_utc,
            texto=texto,
            datos=datos,
            parametros=parametros,
        )
    except Exception as exc:
        print(f"    Open-Meteo no disponible: {exc}")
        print("    Usando proveedor de respaldo MET Norway...")

    texto, datos, parametros = pedir_met_no(lat, lon)
    return normalizar_met_no(
        lote_id=lote_id,
        item=item,
        ahora_utc=ahora_utc,
        objetivo_utc=objetivo_utc,
        texto=texto,
        datos=datos,
        parametros=parametros,
    )


def main() -> None:
    modo = os.environ.get("CLIMA_MODO", "estadios").strip().lower()
    if modo not in {"estadios", "proximos"}:
        raise RuntimeError("CLIMA_MODO debe ser 'estadios' o 'proximos'.")

    dias = int(os.environ.get("CLIMA_DIAS", "7"))

    api = ApiIngesta()

    print("Comprobando puente IONOS...")
    health = api.health()
    print(
        "  OK -> BD:",
        health.get("database"),
        "| MariaDB:",
        health.get("db_version"),
        "| PHP:",
        health.get("php"),
    )

    items = api.contexto_clima(modo=modo, dias=dias)
    print(f"Contextos de clima recibidos: {len(items)} (modo={modo})")

    lote_id = api.iniciar_lote(
        fuente="weather-multi",
        tipo_fuente="api",
        notas=f"GitHub Actions; clima robusto; modo={modo}",
    )
    print(f"Lote RAW abierto: {lote_id}")

    procesados = 0
    errores = []
    fuentes_usadas = []

    for item in items:
        ahora_utc = datetime.now(timezone.utc)
        objetivo_utc = objetivo_para_item(item, ahora_utc, modo)
        nombre = item.get("estadio") or f"estadio_id={item.get('estadio_id')}"

        print(
            f"- {nombre}"
            + (
                f" | partido_id={item.get('partido_id')}"
                if item.get("partido_id")
                else ""
            )
            + f" | objetivo UTC={objetivo_utc.isoformat()}"
        )

        try:
            payload = obtener_payload_clima(
                lote_id=lote_id,
                item=item,
                ahora_utc=ahora_utc,
                objetivo_utc=objetivo_utc,
            )
            resultado = api.guardar_clima(payload)
            procesados += 1
            fuentes_usadas.append(payload["fuente"])
            print(
                f"  guardado [{payload['fuente']}] -> RAW "
                f"{resultado.get('respuesta_raw_id')} | previsión "
                f"{resultado.get('prevision_id')}"
            )
        except Exception as exc:
            errores.append(f"{nombre}: {exc}")
            print(f"  ERROR definitivo: {exc}")

    if errores and procesados:
        estado = "parcial"
    elif errores:
        estado = "error"
    else:
        estado = "completado"

    notas = (
        f"Procesados={procesados}; errores={len(errores)}; "
        f"fuentes={','.join(sorted(set(fuentes_usadas))) or 'ninguna'}."
    )
    if errores:
        notas += " " + " | ".join(errores[:3])

    api.finalizar_lote(lote_id, estado=estado, notas=notas)

    if errores:
        raise RuntimeError(
            f"Ingesta con {len(errores)} error(es); "
            f"procesados={procesados}. Primer error: {errores[0]}"
        )

    print(
        f"Ingesta completada correctamente. Registros={procesados}; "
        f"fuentes={','.join(sorted(set(fuentes_usadas)))}"
    )


if __name__ == "__main__":
    main()
