"""
Tests locales sin Internet para las funciones puras del ingestor.
Ejecutar:
    python ingestion/test_local_clima.py
"""

from datetime import datetime, timezone

from clima_open_meteo import punto_horario_mas_cercano, construir_payload_guardado


def test_punto_cercano():
    hourly = {
        "time": [
            "2026-09-04T04:00",
            "2026-09-04T05:00",
            "2026-09-04T06:00",
        ]
    }
    objetivo = datetime(2026, 9, 4, 5, 20, tzinfo=timezone.utc)
    assert punto_horario_mas_cercano(hourly, objetivo) == 1


def test_payload():
    datos = {
        "hourly": {
            "time": ["2026-09-04T05:00"],
            "temperature_2m": [22.1],
            "apparent_temperature": [21.8],
            "precipitation": [0.2],
            "precipitation_probability": [35],
            "snowfall": [0.0],
            "wind_speed_10m": [12.5],
            "wind_gusts_10m": [20.0],
            "relative_humidity_2m": [68],
            "surface_pressure": [1012.3],
            "cloud_cover": [55],
            "visibility": [18000],
        }
    }
    ahora = datetime(2026, 9, 3, 5, 0, tzinfo=timezone.utc)
    objetivo = datetime(2026, 9, 4, 5, 0, tzinfo=timezone.utc)

    payload = construir_payload_guardado(
        lote_id=7,
        item={
            "estadio_id": 1,
            "partido_id": None,
            "latitud": "43.264100",
            "longitud": "-2.949800",
        },
        ahora_utc=ahora,
        objetivo_utc=objetivo,
        payload_texto='{"ok":true}',
        datos=datos,
        parametros={"latitude": 43.2641, "longitude": -2.9498},
    )

    assert payload["lote_id"] == 7
    assert payload["estadio_id"] == 1
    assert payload["horas_antelacion"] == 24
    assert payload["temperatura_c"] == 22.1
    assert payload["visibilidad_km"] == 18.0
    assert payload["raw_payload"] == '{"ok":true}'


if __name__ == "__main__":
    test_punto_cercano()
    test_payload()
    print("Tests locales OK.")
