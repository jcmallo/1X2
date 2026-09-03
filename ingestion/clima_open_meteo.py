"""
Ingesta de clima (Open-Meteo) para los estadios ya cargados en nucleo_estadios.

Qué hace, paso a paso:
  1. Abre un "lote de ingesta" en bruto_lotes_ingesta (trazabilidad de esta ejecución).
  2. Para cada estadio con latitud/longitud, pide a Open-Meteo la previsión
     horaria de los próximos días.
  3. Guarda la respuesta CRUDA tal cual en bruto_respuestas_api (capa RAW,
     nunca se borra ni se modifica).
  4. Extrae el punto horario más cercano a "ahora + 24h" (el horizonte
     T-24h que usamos en todo el proyecto para evitar fuga de datos) y
     lo guarda en clima_previsiones, enlazado a la respuesta cruda de la
     que salió.

No necesita ninguna clave de API (Open-Meteo es gratuito y abierto).

Uso:
    python ingestion/clima_open_meteo.py
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import requests

from db import obtener_conexion

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HORAS_OBJETIVO = 24  # horizonte T-24h


def obtener_estadios(conexion):
    with conexion.cursor() as cur:
        cur.execute(
            """
            SELECT estadio_id, nombre, latitud, longitud
            FROM nucleo_estadios
            WHERE latitud IS NOT NULL AND longitud IS NOT NULL
            """
        )
        return cur.fetchall()


def pedir_prevision(latitud, longitud):
    """Llama a Open-Meteo y devuelve (payload_texto, json_parseado)."""
    parametros = {
        "latitude": latitud,
        "longitude": longitud,
        "hourly": "temperature_2m,precipitation,precipitation_probability,"
                  "wind_speed_10m,wind_gusts_10m,relative_humidity_2m,"
                  "surface_pressure,cloud_cover,visibility",
        "forecast_days": 3,
        "timezone": "UTC",
    }
    respuesta = requests.get(OPEN_METEO_URL, params=parametros, timeout=15)
    respuesta.raise_for_status()
    return respuesta.text, respuesta.json()


def punto_horario_mas_cercano(datos_horarios, objetivo_dt):
    """De la lista horaria de Open-Meteo, devuelve el índice cuya hora
    está más cerca de objetivo_dt (todo en UTC)."""
    horas = datos_horarios["time"]  # lista de strings ISO, p.ej. '2026-09-04T18:00'
    mejor_indice = 0
    mejor_diferencia = None
    for i, hora_str in enumerate(horas):
        hora_dt = datetime.fromisoformat(hora_str).replace(tzinfo=timezone.utc)
        diferencia = abs((hora_dt - objetivo_dt).total_seconds())
        if mejor_diferencia is None or diferencia < mejor_diferencia:
            mejor_diferencia = diferencia
            mejor_indice = i
    return mejor_indice


def procesar_estadio(conexion, lote_id, estadio, ahora):
    estadio_id = estadio["estadio_id"]
    nombre = estadio["nombre"]
    print(f"  - {nombre} (estadio_id={estadio_id})")

    payload_texto, datos = pedir_prevision(estadio["latitud"], estadio["longitud"])
    hash_payload = hashlib.sha256(payload_texto.encode("utf-8")).hexdigest()

    with conexion.cursor() as cur:
        # 1) Guardar la respuesta cruda (capa RAW)
        cur.execute(
            """
            INSERT INTO bruto_respuestas_api
                (lote_id, fuente, endpoint, parametros_solicitud, solicitado_en,
                 respondido_en, codigo_http, payload, hash_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                lote_id,
                "open-meteo",
                OPEN_METEO_URL,
                json.dumps({"latitude": float(estadio["latitud"]), "longitude": float(estadio["longitud"])}),
                ahora,
                datetime.now(timezone.utc).replace(tzinfo=None),
                200,
                payload_texto,
                hash_payload,
            ),
        )
        respuesta_id = cur.lastrowid

        # 2) Extraer el punto horario en el horizonte T-24h y guardarlo en CLEAN
        objetivo = ahora.replace(tzinfo=timezone.utc) + timedelta(hours=HORAS_OBJETIVO)
        horario = datos["hourly"]
        idx = punto_horario_mas_cercano(horario, objetivo)

        cur.execute(
            """
            INSERT INTO clima_previsiones
                (estadio_id, partido_id, prevision_generada_en, prevista_para,
                 horas_antelacion, temperatura_c, lluvia_mm, probabilidad_lluvia_pct,
                 humedad_pct, velocidad_viento_kmh, rachas_viento_kmh, presion_hpa,
                 nubosidad_pct, visibilidad_km, fuente, origen_respuesta_id)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                estadio_id,
                ahora,
                horario["time"][idx],
                HORAS_OBJETIVO,
                horario["temperature_2m"][idx],
                horario["precipitation"][idx],
                horario["precipitation_probability"][idx],
                horario["relative_humidity_2m"][idx],
                horario["wind_speed_10m"][idx],
                horario["wind_gusts_10m"][idx],
                horario["surface_pressure"][idx],
                horario["cloud_cover"][idx],
                (horario["visibility"][idx] / 1000.0) if horario.get("visibility") else None,
                "open-meteo",
                respuesta_id,
            ),
        )


def main():
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    conexion = obtener_conexion()
    try:
        with conexion.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bruto_lotes_ingesta (fuente, tipo_fuente, iniciado_en, estado)
                VALUES (%s, %s, %s, %s)
                """,
                ("open-meteo", "api", ahora, "en_curso"),
            )
            lote_id = cur.lastrowid

        estadios = obtener_estadios(conexion)
        print(f"Estadios con coordenadas: {len(estadios)}")

        for estadio in estadios:
            procesar_estadio(conexion, lote_id, estadio, ahora)

        with conexion.cursor() as cur:
            cur.execute(
                """
                UPDATE bruto_lotes_ingesta
                SET estado = %s, finalizado_en = %s
                WHERE lote_id = %s
                """,
                ("ok", datetime.now(timezone.utc).replace(tzinfo=None), lote_id),
            )

        conexion.commit()
        print("Ingesta de clima completada correctamente.")

    except Exception:
        conexion.rollback()
        raise
    finally:
        conexion.close()


if __name__ == "__main__":
    main()
