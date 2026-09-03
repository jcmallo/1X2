"""
Ingestor de previsión meteorológica para Quiniela 1X2.

Arquitectura:
    GitHub Actions (Python)
        -> Open-Meteo
        -> API PHP privada en IONOS
        -> MariaDB

GitHub NO conoce las credenciales de MariaDB.

MODOS
-----
CLIMA_MODO=estadios  (por defecto, para la primera prueba)
    Procesa todos los estadios con coordenadas y guarda una previsión
    para ahora + 24 horas, sin asociarla a partido_id.

CLIMA_MODO=proximos
    Procesa partidos PROGRAMADOS de los próximos CLIMA_DIAS días.
    La previsión se toma para la hora real de inicio del partido y
    horas_antelacion se calcula respecto al momento de la consulta.

Una vez comprobado el circuito completo, el modo recomendado para producción
es "proximos".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from api_client import ApiIngesta


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
ZONA_PARTIDOS = ZoneInfo("Europe/Madrid")


def utc_naive(dt: datetime) -> str:
    """Devuelve YYYY-mm-dd HH:MM:SS en UTC, sin offset, para el esquema DATETIME."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def interpretar_inicio_partido(fecha_hora_inicio: str) -> datetime:
    """
    nucleo_partidos.fecha_hora_inicio es DATETIME sin zona.
    Para competiciones españolas actuales lo interpretamos como Europe/Madrid
    y lo convertimos a UTC para compararlo con Open-Meteo (timezone=UTC).
    """
    dt_local = datetime.fromisoformat(fecha_hora_inicio)
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=ZONA_PARTIDOS)
    return dt_local.astimezone(timezone.utc)


def pedir_prevision(latitud: float, longitud: float) -> tuple[str, dict, dict]:
    parametros = {
        "latitude": float(latitud),
        "longitude": float(longitud),
        "hourly": (
            "temperature_2m,apparent_temperature,precipitation,"
            "precipitation_probability,snowfall,wind_speed_10m,"
            "wind_gusts_10m,relative_humidity_2m,surface_pressure,"
            "cloud_cover,visibility"
        ),
        "forecast_days": 14,
        "timezone": "UTC",
    }

    respuesta = requests.get(
        OPEN_METEO_URL,
        params=parametros,
        timeout=25,
        headers={"User-Agent": "quiniela-1x2-ingestor/1.0"},
    )
    respuesta.raise_for_status()
    return respuesta.text, respuesta.json(), parametros


def punto_horario_mas_cercano(datos_horarios: dict, objetivo_utc: datetime) -> int:
    horas = datos_horarios.get("time")
    if not horas:
        raise RuntimeError("Open-Meteo no devolvió horas en hourly.time")

    mejor_indice = 0
    mejor_diferencia = None

    for i, hora_str in enumerate(horas):
        hora_dt = datetime.fromisoformat(hora_str).replace(tzinfo=timezone.utc)
        diferencia = abs((hora_dt - objetivo_utc).total_seconds())
        if mejor_diferencia is None or diferencia < mejor_diferencia:
            mejor_diferencia = diferencia
            mejor_indice = i

    return mejor_indice


def valor(horario: dict, clave: str, idx: int):
    lista = horario.get(clave)
    if not isinstance(lista, list) or idx >= len(lista):
        return None
    return lista[idx]


def construir_payload_guardado(
    *,
    lote_id: int,
    item: dict,
    ahora_utc: datetime,
    objetivo_utc: datetime,
    payload_texto: str,
    datos: dict,
    parametros: dict,
) -> dict:
    horario = datos.get("hourly")
    if not isinstance(horario, dict):
        raise RuntimeError("Open-Meteo no devolvió un objeto 'hourly' válido.")

    idx = punto_horario_mas_cercano(horario, objetivo_utc)
    prevista_str = horario["time"][idx]
    prevista_utc = datetime.fromisoformat(prevista_str).replace(tzinfo=timezone.utc)

    # Diferencia real entre el momento de consulta y el instante previsto.
    horas_antelacion = max(
        0,
        int(round((prevista_utc - ahora_utc).total_seconds() / 3600)),
    )

    visibilidad_m = valor(horario, "visibility", idx)

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
        "temperatura_c": valor(horario, "temperature_2m", idx),
        "sensacion_termica_c": valor(horario, "apparent_temperature", idx),
        "lluvia_mm": valor(horario, "precipitation", idx),
        "probabilidad_lluvia_pct": valor(
            horario, "precipitation_probability", idx
        ),
        "humedad_pct": valor(horario, "relative_humidity_2m", idx),
        "velocidad_viento_kmh": valor(horario, "wind_speed_10m", idx),
        "rachas_viento_kmh": valor(horario, "wind_gusts_10m", idx),
        "presion_hpa": valor(horario, "surface_pressure", idx),
        "nubosidad_pct": valor(horario, "cloud_cover", idx),
        "visibilidad_km": (
            float(visibilidad_m) / 1000.0
            if visibilidad_m is not None
            else None
        ),
        "nieve_mm": valor(horario, "snowfall", idx),
        "fuente": "open-meteo",
        "endpoint": OPEN_METEO_URL,
        "parametros_solicitud": parametros,
        "solicitado_en": utc_naive(ahora_utc),
        "respondido_en": utc_naive(datetime.now(timezone.utc)),
        "codigo_http": 200,
        # Se manda el texto exacto para conservar la capa RAW tal como llegó.
        "raw_payload": payload_texto,
    }


def objetivo_para_item(item: dict, ahora_utc: datetime, modo: str) -> datetime:
    if modo == "proximos" and item.get("fecha_hora_inicio"):
        return interpretar_inicio_partido(str(item["fecha_hora_inicio"]))
    return ahora_utc + timedelta(hours=24)


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
        fuente="open-meteo",
        tipo_fuente="api",
        notas=f"GitHub Actions; modo clima={modo}",
    )
    print(f"Lote RAW abierto: {lote_id}")

    procesados = 0
    errores = []

    try:
        for item in items:
            ahora_utc = datetime.now(timezone.utc)
            objetivo_utc = objetivo_para_item(item, ahora_utc, modo)

            nombre = item.get("estadio") or f"estadio_id={item.get('estadio_id')}"
            partido = item.get("partido_id")
            print(
                f"- {nombre}"
                + (f" | partido_id={partido}" if partido else "")
                + f" | objetivo UTC={objetivo_utc.isoformat()}"
            )

            try:
                payload_texto, datos, parametros = pedir_prevision(
                    float(item["latitud"]),
                    float(item["longitud"]),
                )
                payload = construir_payload_guardado(
                    lote_id=lote_id,
                    item=item,
                    ahora_utc=ahora_utc,
                    objetivo_utc=objetivo_utc,
                    payload_texto=payload_texto,
                    datos=datos,
                    parametros=parametros,
                )
                resultado = api.guardar_clima(payload)
                procesados += 1
                print(
                    "  guardado -> RAW",
                    resultado.get("respuesta_raw_id"),
                    "| previsión",
                    resultado.get("prevision_id"),
                )
            except Exception as exc:
                errores.append(f"{nombre}: {exc}")
                print(f"  ERROR: {exc}")

        if errores and procesados:
            estado = "parcial"
        elif errores:
            estado = "error"
        else:
            estado = "completado"

        notas = (
            f"Procesados={procesados}; errores={len(errores)}."
            + ((" " + " | ".join(errores[:5])) if errores else "")
        )
        api.finalizar_lote(lote_id, estado=estado, notas=notas)

        if errores:
            raise RuntimeError(
                f"La ingesta terminó con {len(errores)} error(es). "
                f"Procesados correctamente: {procesados}. "
                f"Primer error: {errores[0]}"
            )

        print(
            f"Ingesta completada correctamente. "
            f"Registros de clima guardados: {procesados}."
        )

    except Exception:
        # Si el fallo fue antes de finalizar el lote, intentamos dejarlo marcado.
        try:
            api.finalizar_lote(
                lote_id,
                estado="error",
                notas="La ejecución terminó con una excepción no controlada.",
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
