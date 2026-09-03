"""
Completa automáticamente coordenadas de estadios que todavía no las tienen.

Fuente:
    OpenStreetMap Nominatim

Diseño:
- Solo solicita estadios SIN latitud/longitud.
- Máximo 1 petición/segundo al servicio público.
- Conserva la respuesta RAW en MariaDB.
- No marca las coordenadas como verificadas manualmente:
  coordenadas_verificadas = 0.
- Nunca sobrescribe coordenadas existentes.
- Si no hay una coincidencia suficientemente clara, omite el estadio.

Arquitectura:
GitHub Actions -> Nominatim -> API PHP IONOS -> MariaDB
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

import requests

from api_client import ApiIngesta


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = (
    "quiniela-1x2-stadium-geocoder/1.0 "
    "https://1x2.juancarlosmallo.com"
)
MIN_INTERVALO = 1.1

session = requests.Session()
session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "es",
    }
)

_ultima_peticion = 0.0


def ahora_sql() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def normalizar(texto: str | None) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(
        c for c in texto if not unicodedata.combining(c)
    ).casefold()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def primer_equipo(contexto: str | None) -> str | None:
    if not contexto:
        return None
    primera = contexto.split("|", 1)[0].strip()
    # "Valencia CF [LaLiga]" -> "Valencia CF"
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", primera).strip() or None


def esperar_rate_limit() -> None:
    global _ultima_peticion
    transcurrido = time.monotonic() - _ultima_peticion
    if transcurrido < MIN_INTERVALO:
        time.sleep(MIN_INTERVALO - transcurrido)


def consultar_nominatim(
    query: str,
) -> tuple[str, list[dict], dict, str, str]:
    global _ultima_peticion

    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 5,
        "countrycodes": "es",
        "addressdetails": 1,
        "namedetails": 1,
        "extratags": 1,
    }

    esperar_rate_limit()
    solicitado = ahora_sql()
    r = session.get(
        NOMINATIM_URL,
        params=params,
        timeout=(10, 30),
    )
    _ultima_peticion = time.monotonic()
    respondido = ahora_sql()

    if r.status_code != 200:
        raise RuntimeError(
            f"Nominatim HTTP {r.status_code}: {r.text[:250]}"
        )

    datos = r.json()
    if not isinstance(datos, list):
        raise RuntimeError("Nominatim devolvió un formato inesperado.")

    return r.text, datos, params, solicitado, respondido


def nombre_candidato(c: dict) -> str:
    namedetails = c.get("namedetails") or {}
    for clave in ("name", "name:es", "official_name", "short_name"):
        if namedetails.get(clave):
            return str(namedetails[clave])
    return str(c.get("name") or "")


def puntuar(
    candidato: dict,
    estadio: str,
    ciudad: str | None,
    equipo: str | None,
) -> int:
    score = 0

    nom_estadio = normalizar(estadio)
    nombre = normalizar(nombre_candidato(candidato))
    display = normalizar(candidato.get("display_name"))

    if nombre == nom_estadio and nombre:
        score += 8
    elif nom_estadio and nom_estadio in display:
        score += 5
    elif nombre and (
        nombre in nom_estadio
        or nom_estadio in nombre
    ):
        score += 3

    clase = normalizar(candidato.get("class"))
    tipo = normalizar(candidato.get("type"))

    if tipo in {"stadium", "sports centre", "pitch"}:
        score += 4
    elif clase == "leisure":
        score += 2

    if ciudad and normalizar(ciudad) in display:
        score += 3

    if equipo:
        palabras = [
            p for p in normalizar(equipo).split()
            if len(p) >= 4 and p not in {"club", "futbol", "football"}
        ]
        if any(p in display for p in palabras):
            score += 1

    if "espana" in display or "spain" in display:
        score += 1

    return score


def extraer_ciudad(candidato: dict) -> str | None:
    a = candidato.get("address") or {}
    for clave in (
        "city",
        "town",
        "municipality",
        "village",
        "city_district",
        "county",
    ):
        valor = a.get(clave)
        if valor:
            return str(valor).strip()
    return None


def buscar_estadio(item: dict) -> dict | None:
    estadio = str(item["nombre"]).strip()
    ciudad = (
        str(item["ciudad"]).strip()
        if item.get("ciudad")
        else None
    )
    equipo = primer_equipo(item.get("contexto_equipos"))

    queries = []

    if ciudad:
        queries.append(f"{estadio}, {ciudad}, España")

    if equipo:
        queries.append(f"{estadio}, {equipo}, España")

    queries.append(f"{estadio}, España")

    vistas = set()

    for query in queries:
        if normalizar(query) in vistas:
            continue
        vistas.add(normalizar(query))

        raw, datos, params, solicitado, respondido = consultar_nominatim(query)

        if not datos:
            continue

        puntuados = [
            (
                puntuar(c, estadio, ciudad, equipo),
                c,
            )
            for c in datos
        ]
        puntuados.sort(key=lambda x: x[0], reverse=True)

        score, mejor = puntuados[0]

        # Umbral prudente: si no estamos razonablemente seguros,
        # preferimos dejar las coordenadas pendientes.
        if score < 6:
            continue

        return {
            "query": query,
            "score": score,
            "candidato": mejor,
            "raw": raw,
            "params": params,
            "solicitado_en": solicitado,
            "respondido_en": respondido,
        }

    return None


def main() -> None:
    api = ApiIngesta()

    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    items = api.contexto_estadios(
        solo_pendientes=True,
        limite=int(os.environ.get("ESTADIOS_LIMITE", "100")),
    )

    print("Estadios pendientes de coordenadas:", len(items))

    if not items:
        print("No hay trabajo pendiente.")
        return

    lote = api.iniciar_lote(
        fuente="nominatim",
        tipo_fuente="api",
        notas="Geocodificación automática de estadios sin coordenadas.",
    )
    print("Lote abierto:", lote)

    actualizados = 0
    omitidos = 0
    errores = []

    for item in items:
        estadio_id = int(item["estadio_id"])
        nombre = str(item["nombre"])

        print(f"- estadio_id={estadio_id} | {nombre}")

        try:
            resultado = buscar_estadio(item)

            if resultado is None:
                omitidos += 1
                print("  OMITIDO: no hay coincidencia suficientemente clara.")
                continue

            candidato = resultado["candidato"]
            ciudad = extraer_ciudad(candidato)

            lat = float(candidato["lat"])
            lon = float(candidato["lon"])
            score = int(resultado["score"])
            display = str(candidato.get("display_name") or "")[:240]

            payload = {
                "lote_id": lote,
                "estadio_id": estadio_id,
                "ciudad": ciudad,
                "latitud": lat,
                "longitud": lon,
                # Automático != verificado manualmente.
                "coordenadas_verificadas": 0,
                "sobrescribir": 0,
                "fuente_coordenadas": (
                    "OpenStreetMap/Nominatim; "
                    f"query={resultado['query']}"
                )[:500],
                "notas_calidad": (
                    f"Geocodificación automática; score={score}; "
                    f"{display}"
                )[:500],
                "fuente_raw": "nominatim",
                "endpoint": NOMINATIM_URL,
                "parametros_solicitud": resultado["params"],
                "solicitado_en": resultado["solicitado_en"],
                "respondido_en": resultado["respondido_en"],
                "codigo_http": 200,
                "raw_payload": resultado["raw"],
            }

            res = api.guardar_estadio(payload)

            if res.get("accion") == "actualizado":
                actualizados += 1
                print(
                    f"  OK -> {lat:.6f}, {lon:.6f}"
                    + (f" | {ciudad}" if ciudad else "")
                    + f" | score={score}"
                )
            else:
                omitidos += 1
                print("  SIN CAMBIOS:", res.get("motivo"))

        except Exception as exc:
            errores.append(f"{nombre}: {exc}")
            print("  ERROR:", exc)

    if errores and actualizados:
        estado = "parcial"
    elif errores:
        estado = "error"
    else:
        estado = "completado"

    notas = (
        f"actualizados={actualizados}; "
        f"omitidos={omitidos}; "
        f"errores={len(errores)}"
    )
    if errores:
        notas += " | " + " | ".join(errores[:3])

    api.finalizar_lote(
        lote,
        estado=estado,
        notas=notas,
    )

    print(
        "\nResumen:"
        f" actualizados={actualizados},"
        f" omitidos={omitidos},"
        f" errores={len(errores)}"
    )

    if errores:
        raise RuntimeError(
            f"Geocodificación terminó con {len(errores)} error(es). "
            f"Primer error: {errores[0]}"
        )


if __name__ == "__main__":
    main()
