"""Prueba local: simula la respuesta de Open-Meteo y ejecuta el resto del
pipeline real (RAW + CLEAN) contra una base de datos MariaDB de verdad."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__) + "/../ingestion")

os.environ["DB_HOST"] = "127.0.0.1"
os.environ["DB_PORT"] = "3306"
os.environ["DB_USER"] = "test_user"
os.environ["DB_PASSWORD"] = "test_pass_123"
os.environ["DB_NAME"] = "quiniela_test"

import clima_open_meteo as mod  # noqa: E402


def payload_simulado():
    """Respuesta realista de Open-Meteo: 72 horas a partir de ahora."""
    inicio = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    horas = [(inicio + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(72)]
    n = len(horas)
    datos = {
        "latitude": 43.26,
        "longitude": -2.95,
        "hourly": {
            "time": horas,
            "temperature_2m": [18.5 + (i % 5) for i in range(n)],
            "precipitation": [0.0 if i % 7 else 1.2 for i in range(n)],
            "precipitation_probability": [10 if i % 7 else 60 for i in range(n)],
            "relative_humidity_2m": [65 for _ in range(n)],
            "wind_speed_10m": [12.4 for _ in range(n)],
            "wind_gusts_10m": [22.1 for _ in range(n)],
            "surface_pressure": [1013.2 for _ in range(n)],
            "cloud_cover": [40 for _ in range(n)],
            "visibility": [24140.0 for _ in range(n)],
        },
    }
    return json.dumps(datos), datos


def falso_pedir_prevision(latitud, longitud):
    return payload_simulado()


with patch.object(mod, "pedir_prevision", side_effect=falso_pedir_prevision):
    mod.main()

# Verificación
conexion = mod.obtener_conexion()
with conexion.cursor() as cur:
    cur.execute("SELECT COUNT(*) AS n FROM bruto_lotes_ingesta WHERE fuente='open-meteo'")
    print("Lotes de ingesta open-meteo:", cur.fetchone()["n"])
    cur.execute("SELECT COUNT(*) AS n FROM bruto_respuestas_api WHERE fuente='open-meteo'")
    print("Respuestas crudas guardadas:", cur.fetchone()["n"])
    cur.execute(
        """
        SELECT e.nombre, c.prevista_para, c.horas_antelacion, c.temperatura_c,
               c.lluvia_mm, c.probabilidad_lluvia_pct, c.origen_respuesta_id
        FROM clima_previsiones c
        JOIN nucleo_estadios e ON e.estadio_id = c.estadio_id
        """
    )
    for fila in cur.fetchall():
        print(fila)
conexion.close()
