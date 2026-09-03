"""
Carga las plantillas actuales 2026-27 de:
- LALIGA EA SPORTS
- LALIGA HYPERMOTION
- Liga F

Corrección v3:
- Corrige reejecuciones/idempotencia del guardado PHP.
- Liga F: mantiene el parser HTML que ya funcionó.
- LALIGA/Segunda: usa players/stats como plantilla completa por temporada
  y squad solo para enriquecer biografía.
- LALIGA/Segunda: usa el JSON que alimenta laliga.com
  (Azure APIM / public-service), porque la tabla visible de estadísticas
  no expone enlaces /jugador/ en el HTML que recibe requests/GitHub Actions.

La clave pública de APIM se intenta obtener de __NEXT_DATA__ de laliga.com.
Si no aparece, existe un fallback actual; ante 401 se intenta refrescarla.

No inventa jugadores. Si una plantilla obtiene menos de 15 futbolistas,
se rechaza para evitar cerrar pertenencias por una respuesta incompleta.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from api_client import ApiIngesta


TEMPORADA = "2026-27"
LIGAF_ANIO_URL = "2027"

LALIGA_APIM_BASE = "https://apim.laliga.com/public-service"
# Fallback público observado en laliga.com. La función intenta extraer
# primero el valor actual desde __NEXT_DATA__ para no depender de él.
LALIGA_APIM_KEY_FALLBACK = "c13c3a8e2f6b46da9c5c425cf61fab3e"

POSICIONES_APIM = {
    1: "Portero",
    2: "Defensa",
    3: "Centrocampista",
    4: "Delantero",
}

session = requests.Session()
session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (compatible; Quiniela1X2/1.0; "
            "+https://1x2.juancarlosmallo.com)"
        ),
        "Accept-Language": "es-ES,es;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/json",
    }
)

_apim_key_cache: str | None = None
_stats_cache: dict[str, list[dict]] = {}
_stats_raw_guardados: set[str] = set()


def ahora_sql() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def slugify(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", valor)
    texto = "".join(
        c for c in texto
        if not unicodedata.combining(c)
    ).lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto


def pedir_html(url: str, intentos: int = 3) -> tuple[str, str]:
    ultimo = None

    for intento in range(1, intentos + 1):
        try:
            r = session.get(url, timeout=(10, 45))
            if r.status_code == 200:
                return r.text, r.url

            if r.status_code in {429, 500, 502, 503, 504} and intento < intentos:
                espera = 2 ** (intento - 1)
                print(
                    f"    HTTP {r.status_code}; "
                    f"reintento en {espera}s..."
                )
                time.sleep(espera)
                continue

            r.raise_for_status()

        except (requests.Timeout, requests.ConnectionError) as exc:
            ultimo = exc
            if intento < intentos:
                espera = 2 ** (intento - 1)
                time.sleep(espera)
                continue
            raise

    raise RuntimeError(f"No se pudo descargar {url}: {ultimo}")


def guardar_raw(
    api: ApiIngesta,
    lote_id: int,
    fuente: str,
    url: str,
    tipo: str,
    contenido: str,
) -> None:
    api.guardar_documento(
        {
            "lote_id": lote_id,
            "fuente": fuente,
            "url": url,
            "tipo_contenido": tipo,
            "obtenido_en": ahora_sql(),
            "contenido": contenido,
            "hash_contenido": hashlib.sha256(
                contenido.encode("utf-8")
            ).hexdigest(),
        }
    )


def _buscar_clave_recursiva(obj: Any, nombre: str) -> str | None:
    if isinstance(obj, dict):
        valor = obj.get(nombre)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()

        for v in obj.values():
            encontrado = _buscar_clave_recursiva(v, nombre)
            if encontrado:
                return encontrado

    elif isinstance(obj, list):
        for v in obj:
            encontrado = _buscar_clave_recursiva(v, nombre)
            if encontrado:
                return encontrado

    return None


def extraer_apim_key_desde_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")

    if script:
        try:
            data = json.loads(script.string or script.get_text())
            key = _buscar_clave_recursiva(data, "backendSubscription")
            if key:
                return key
        except Exception:
            pass

    patrones = [
        r'"backendSubscription"\s*:\s*"([^"]+)"',
        r'backendSubscription\\?":\\?"([^"\\]+)',
    ]

    for patron in patrones:
        m = re.search(patron, html)
        if m:
            return m.group(1)

    return None


def obtener_apim_key(forzar_refresco: bool = False) -> str:
    global _apim_key_cache

    if _apim_key_cache and not forzar_refresco:
        return _apim_key_cache

    urls = [
        "https://www.laliga.com/",
        "https://www.laliga.com/clubes/athletic-club/estadisticas",
    ]

    for url in urls:
        try:
            html, _ = pedir_html(url, intentos=2)
            key = extraer_apim_key_desde_html(html)
            if key:
                _apim_key_cache = key
                print("    APIM LALIGA: clave pública obtenida de laliga.com")
                return key
        except Exception:
            continue

    _apim_key_cache = LALIGA_APIM_KEY_FALLBACK
    print("    APIM LALIGA: usando fallback público")
    return _apim_key_cache


def pedir_json_laliga(
    path: str,
    *,
    intentos: int = 4,
) -> tuple[dict, str]:
    url = LALIGA_APIM_BASE + path
    ultimo_error: Exception | None = None

    for intento in range(1, intentos + 1):
        key = obtener_apim_key(forzar_refresco=False)

        try:
            r = session.get(
                url,
                headers={
                    "Ocp-Apim-Subscription-Key": key,
                    "Accept": "application/json",
                },
                timeout=(10, 45),
            )

            if r.status_code == 200:
                data = r.json()
                if not isinstance(data, dict):
                    raise RuntimeError(
                        f"JSON inesperado en {path}: "
                        f"{type(data).__name__}"
                    )
                return data, r.url

            if r.status_code in {401, 403}:
                # La clave puede rotar. Refrescar desde la web y reintentar.
                obtener_apim_key(forzar_refresco=True)

            elif r.status_code in {429, 500, 502, 503, 504}:
                pass
            else:
                raise RuntimeError(
                    f"LALIGA APIM HTTP {r.status_code}: "
                    f"{r.text[:250]}"
                )

        except (
            requests.Timeout,
            requests.ConnectionError,
            requests.JSONDecodeError,
            RuntimeError,
        ) as exc:
            ultimo_error = exc

        if intento < intentos:
            espera = min(8, 2 ** (intento - 1))
            time.sleep(espera)

    raise RuntimeError(
        f"No se pudo obtener LALIGA APIM {path}: {ultimo_error}"
    )


def normalizar_fecha_api(valor: Any) -> str | None:
    if not isinstance(valor, str):
        return None

    v = valor.strip()
    if not v:
        return None

    # ISO
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return "-".join(m.groups())

    # dd-mm-yyyy o dd/mm/yyyy
    m = re.match(r"^(\d{2})[-/](\d{2})[-/](\d{4})", v)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm}-{dd}"

    return None


def normalizar_altura_api(valor: Any) -> int | None:
    if valor is None:
        return None

    if isinstance(valor, (int, float)):
        n = float(valor)
        if 1.3 <= n <= 2.3:
            n *= 100
        n_int = round(n)
        return n_int if 130 <= n_int <= 230 else None

    if isinstance(valor, str):
        m = re.search(r"(\d+(?:[.,]\d+)?)", valor)
        if not m:
            return None
        n = float(m.group(1).replace(",", "."))
        if 1.3 <= n <= 2.3:
            n *= 100
        n_int = round(n)
        return n_int if 130 <= n_int <= 230 else None

    return None


def extraer_pais(person: dict) -> str | None:
    country = person.get("country")

    if isinstance(country, dict):
        for campo in ("name", "nickname", "id", "code"):
            valor = country.get(campo)
            if isinstance(valor, str) and valor.strip():
                return valor.strip()[:80]

    if isinstance(country, str) and country.strip():
        return country.strip()[:80]

    nacionalidad = person.get("nationality")
    if isinstance(nacionalidad, str) and nacionalidad.strip():
        return nacionalidad.strip()[:80]

    return None


def nombre_persona(person: dict, item: dict) -> str | None:
    for campo in ("name", "nickname", "full_name"):
        valor = person.get(campo)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()

    partes = []
    for campo in ("firstname", "first_name", "lastname", "last_name"):
        valor = person.get(campo)
        if isinstance(valor, str) and valor.strip():
            partes.append(valor.strip())

    if partes:
        return " ".join(partes)

    for campo in ("name", "nickname"):
        valor = item.get(campo)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()

    return None


def parse_squad_laliga(data: dict) -> list[dict]:
    lista = data.get("squads")

    if not isinstance(lista, list):
        lista = data.get("squad")

    if not isinstance(lista, list):
        lista = data.get("players")

    if not isinstance(lista, list):
        raise RuntimeError(
            "La respuesta de squad no contiene "
            "'squads', 'squad' ni 'players'."
        )

    salida: dict[str, dict] = {}

    for item in lista:
        if not isinstance(item, dict):
            continue

        person = item.get("person")
        if not isinstance(person, dict):
            person = {}

        id_fuente = (
            item.get("opta_id")
            or person.get("opta_id")
            or item.get("id")
            or person.get("id")
        )

        if id_fuente is None:
            continue

        id_fuente = str(id_fuente).strip()
        if not id_fuente:
            continue

        nombre = nombre_persona(person, item)
        if not nombre:
            continue

        posicion_obj = item.get("position")
        posicion = None

        if isinstance(posicion_obj, dict):
            try:
                posicion = POSICIONES_APIM.get(
                    int(posicion_obj.get("id"))
                )
            except (TypeError, ValueError):
                pass

            if posicion is None:
                valor = posicion_obj.get("name")
                if isinstance(valor, str) and valor.strip():
                    posicion = valor.strip()[:30]

        dorsal = item.get("shirt_number")
        if dorsal is None:
            dorsal = item.get("shirt")

        try:
            dorsal = int(dorsal) if dorsal not in (None, "") else None
        except (TypeError, ValueError):
            dorsal = None

        if dorsal is not None and not 0 <= dorsal <= 99:
            dorsal = None

        salida[id_fuente] = {
            "id_fuente": id_fuente,
            "nombre_completo": nombre[:150],
            "fecha_nacimiento": normalizar_fecha_api(
                person.get("date_of_birth")
                or person.get("birth_date")
            ),
            "nacionalidad": extraer_pais(person),
            "altura_cm": normalizar_altura_api(
                person.get("height")
            ),
            "posicion_principal": posicion,
            "dorsal": dorsal,
        }

    return list(salida.values())


def subscription_actual(competicion: str) -> str:
    if competicion == "LaLiga":
        return "laliga-easports-2026"

    if competicion == "Segunda División":
        return "laliga-hypermotion-2026"

    raise ValueError(
        f"No hay subscription LALIGA definida para {competicion}"
    )


def parse_player_stats_equipo(
    registros: list[dict],
    team_slug: str,
) -> list[dict]:
    salida: dict[str, dict] = {}

    for ps in registros:
        if not isinstance(ps, dict):
            continue

        team = ps.get("team")
        if not isinstance(team, dict):
            continue

        slug_equipo = str(team.get("slug") or "").strip()
        if slug_equipo != team_slug:
            continue

        id_fuente = ps.get("opta_id") or ps.get("id")
        nombre = ps.get("name") or ps.get("nickname")

        if id_fuente is None or not isinstance(nombre, str) or not nombre.strip():
            continue

        posicion = None
        pos = ps.get("position")

        if isinstance(pos, dict):
            try:
                posicion = POSICIONES_APIM.get(int(pos.get("id")))
            except (TypeError, ValueError):
                posicion = None

            if posicion is None and isinstance(pos.get("name"), str):
                posicion = pos["name"].strip()[:30]

        dorsal = ps.get("shirt_number")
        try:
            dorsal = int(dorsal) if dorsal not in (None, "") else None
        except (TypeError, ValueError):
            dorsal = None

        country = ps.get("country")
        nacionalidad = None
        if isinstance(country, dict):
            nacionalidad = (
                country.get("name")
                or country.get("id")
                or country.get("code")
            )
        elif isinstance(country, str):
            nacionalidad = country

        salida[str(id_fuente)] = {
            "id_fuente": str(id_fuente),
            "nombre_completo": nombre.strip()[:150],
            "fecha_nacimiento": None,
            "nacionalidad": (
                str(nacionalidad)[:80]
                if nacionalidad
                else None
            ),
            "altura_cm": None,
            "posicion_principal": posicion,
            "dorsal": dorsal,
        }

    return list(salida.values())


def obtener_player_stats_subscription(
    subscription: str,
) -> tuple[list[dict], list[tuple[str, dict]]]:
    if subscription in _stats_cache:
        return _stats_cache[subscription], []

    todos: list[dict] = []
    raws: list[tuple[str, dict]] = []

    for offset in range(0, 2000, 100):
        path = (
            f"/api/v1/subscriptions/{subscription}/players/stats"
            f"?limit=100&offset={offset}"
        )
        data, url = pedir_json_laliga(path)
        raws.append((url, data))

        pagina = data.get("player_stats")
        if not isinstance(pagina, list):
            raise RuntimeError(
                "player/stats no contiene 'player_stats'."
            )

        todos.extend(
            x for x in pagina if isinstance(x, dict)
        )

        total = data.get("total")
        if isinstance(total, int) and len(todos) >= total:
            break

        if len(pagina) < 100:
            break

        time.sleep(0.08)

    _stats_cache[subscription] = todos
    return todos, raws


def siguiente_valor(strings: list[str], etiqueta: str) -> str | None:
    objetivo = etiqueta.casefold()

    for i, s in enumerate(strings):
        if s.casefold().strip(": ") == objetivo:
            for candidato in strings[i + 1:]:
                c = candidato.strip()
                if c:
                    return c
    return None


def extraer_dorsal(strings: list[str], limite: int | None = None) -> int | None:
    zona = strings if limite is None else strings[:limite]

    for s in zona:
        if re.fullmatch(r"\d{1,2}", s.strip()):
            n = int(s)
            if 0 <= n <= 99:
                return n
    return None


def parse_fecha_es(valor: str | None) -> str | None:
    if not valor:
        return None

    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", valor)
    if not m:
        return None

    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def parse_altura(valor: str | None) -> int | None:
    if not valor:
        return None

    m = re.search(r"(\d{3})\s*cm", valor, re.I)
    if not m:
        return None

    n = int(m.group(1))
    return n if 130 <= n <= 230 else None


def parse_ligaf(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jugadores: dict[str, dict] = {}

    posiciones = {
        "portera": "Portero",
        "portero": "Portero",
        "defensa": "Defensa",
        "centro": "Centrocampista",
        "centrocampista": "Centrocampista",
        "delantera": "Delantero",
        "delantero": "Delantero",
    }

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        m = re.search(r"/jugadora/[^/?#]+/(\d+)", href)
        if not m:
            continue

        id_fuente = m.group(1)
        strings = [s.strip() for s in a.stripped_strings if s.strip()]

        if len(strings) < 3:
            continue

        pos_idx = None
        posicion = None

        for i, s in enumerate(strings):
            clave = s.casefold()
            if clave in posiciones:
                pos_idx = i
                posicion = posiciones[clave]
                break

        if pos_idx is None or posicion is None:
            continue

        previos = strings[:pos_idx]

        previos_sin_dorsal = [
            s for s in previos
            if not re.fullmatch(r"\d{1,2}", s)
        ]

        if not previos_sin_dorsal:
            continue

        nombre = max(
            previos_sin_dorsal,
            key=lambda s: (len(s.split()), len(s)),
        ).strip()

        dorsal = extraer_dorsal(previos)

        nacionalidad = siguiente_valor(strings, "Nacionalidad")
        nacimiento = parse_fecha_es(
            siguiente_valor(strings, "Nacimiento")
        )
        altura = parse_altura(
            siguiente_valor(strings, "Altura")
        )

        jugadores[id_fuente] = {
            "id_fuente": id_fuente,
            "nombre_completo": nombre[:150],
            "fecha_nacimiento": nacimiento,
            "nacionalidad": nacionalidad[:80] if nacionalidad else None,
            "altura_cm": altura,
            "posicion_principal": posicion,
            "dorsal": dorsal,
        }

    salida = list(jugadores.values())

    if len(salida) < 15:
        raise RuntimeError(
            f"Parser Liga F solo encontró {len(salida)} jugadoras."
        )

    return salida


def slug_laliga(item: dict) -> str | None:
    for campo in ("id_laliga_com", "id_laliga_legacy"):
        valor = item.get(campo)
        if valor:
            return str(valor).strip()

    return None


def procesar_laliga(
    api: ApiIngesta,
    lote_id: int,
    item: dict,
) -> int:
    team_slug = slug_laliga(item)

    if not team_slug:
        raise RuntimeError(
            "El equipo no tiene ID externo laliga.com/laliga."
        )

    competicion = str(item["competicion"])
    subscription = subscription_actual(competicion)

    # La fuente de verdad de la plantilla de UNA TEMPORADA será players/stats:
    # devuelve todos los futbolistas registrados en esa temporada y permite
    # reconstruir histórico. Se descarga solo una vez por competición.
    registros, raws = obtener_player_stats_subscription(subscription)

    for raw_url, raw_data in raws:
        if raw_url not in _stats_raw_guardados:
            guardar_raw(
                api,
                lote_id,
                "laliga.com-apim",
                raw_url,
                "jugadores_estadisticas_laliga_json",
                json.dumps(
                    raw_data,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            _stats_raw_guardados.add(raw_url)

    jugadores = parse_player_stats_equipo(
        registros,
        team_slug,
    )

    if len(jugadores) < 15:
        raise RuntimeError(
            f"player_stats solo encontró {len(jugadores)} jugadores "
            f"para {team_slug}."
        )

    # Enriquecer los que estén presentes en el squad actual con bio:
    # fecha de nacimiento, nacionalidad y altura.
    # IMPORTANTE: el squad es CURRENT; no se usa como fuente histórica
    # de pertenencia, solo para enriquecer datos personales del mismo opta_id.
    try:
        path_squad = (
            f"/api/v1/teams/{quote(team_slug, safe='-')}/squad"
            f"?subscription={quote(subscription, safe='-')}"
        )
        squad_data, squad_url = pedir_json_laliga(path_squad)

        guardar_raw(
            api,
            lote_id,
            "laliga.com-apim",
            squad_url,
            "plantilla_laliga_squad_json",
            json.dumps(
                squad_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

        squad = {
            j["id_fuente"]: j
            for j in parse_squad_laliga(squad_data)
        }

        for j in jugadores:
            enriquecido = squad.get(j["id_fuente"])
            if not enriquecido:
                continue
            for campo in (
                "fecha_nacimiento",
                "nacionalidad",
                "altura_cm",
                "posicion_principal",
                "dorsal",
            ):
                if j.get(campo) is None and enriquecido.get(campo) is not None:
                    j[campo] = enriquecido[campo]
    except Exception as exc:
        # La plantilla completa ya procede de player_stats.
        # Un fallo de enriquecimiento no debe bloquear la carga.
        print(f"    aviso enriquecimiento squad: {exc}")

    res = api.guardar_plantilla(
        {
            "lote_id": lote_id,
            "equipo_id": int(item["equipo_id"]),
            "fuente": "laliga.com",
            "fecha_plantilla": date.today().isoformat(),
            "jugadores": jugadores,
        }
    )

    print(
        f"    {len(jugadores)} jugadores | "
        f"creados={res.get('jugadores_creados')} | "
        f"vínculos nuevos={res.get('pertenencias_nuevas')}"
    )

    return len(jugadores)


def procesar_ligaf(
    api: ApiIngesta,
    lote_id: int,
    item: dict,
) -> int:
    team_id = str(item.get("id_ligaf") or "").strip()

    if not team_id:
        raise RuntimeError("El equipo no tiene ID externo ligaf.es.")

    slug = slugify(str(item["nombre_canonico"]))
    url = (
        f"https://ligaf.es/equipo/{slug}/{team_id}/"
        f"plantilla/{LIGAF_ANIO_URL}"
    )

    html, final_url = pedir_html(url)

    guardar_raw(
        api,
        lote_id,
        "ligaf.es",
        final_url,
        "plantilla_ligaf",
        html,
    )

    jugadores = parse_ligaf(html)

    res = api.guardar_plantilla(
        {
            "lote_id": lote_id,
            "equipo_id": int(item["equipo_id"]),
            "fuente": "ligaf.es",
            "fecha_plantilla": date.today().isoformat(),
            "jugadores": jugadores,
        }
    )

    print(
        f"    {len(jugadores)} jugadoras | "
        f"creadas={res.get('jugadores_creados')} | "
        f"vínculos nuevos={res.get('pertenencias_nuevas')}"
    )

    return len(jugadores)


def main() -> None:
    api = ApiIngesta()

    health = api.health()
    print(
        "Puente IONOS OK ->",
        health.get("database"),
        health.get("db_version"),
    )

    equipos = api.contexto_plantillas(TEMPORADA)

    print("Equipos a procesar:", len(equipos))

    if not equipos:
        raise RuntimeError(
            "No hay equipos 2026-27 en contexto_plantillas."
        )

    lote_id = api.iniciar_lote(
        fuente="plantillas-oficiales-2026-27",
        tipo_fuente="scraping",
        notas=(
            "Plantillas oficiales LaLiga, Segunda y Liga F. "
            "Snapshot actual. LALIGA vía JSON APIM."
        ),
    )
    print("Lote abierto:", lote_id)

    equipos_ok = 0
    jugadores_total = 0
    errores: list[str] = []

    for item in equipos:
        nombre = str(item["nombre_canonico"])
        competicion = str(item["competicion"])

        print(f"- {competicion} | {nombre}")

        try:
            if competicion == "Liga F":
                n = procesar_ligaf(api, lote_id, item)
            else:
                n = procesar_laliga(api, lote_id, item)

            jugadores_total += n
            equipos_ok += 1

        except Exception as exc:
            errores.append(
                f"{competicion} | {nombre}: {exc}"
            )
            print("    ERROR:", exc)

        time.sleep(0.15)

    if errores and equipos_ok:
        estado = "parcial"
    elif errores:
        estado = "error"
    else:
        estado = "completado"

    notas = (
        f"equipos_ok={equipos_ok}; "
        f"jugadores={jugadores_total}; "
        f"errores={len(errores)}; "
        f"equipos_total={len(equipos)}"
    )

    if errores:
        notas += " | " + " | ".join(errores[:3])

    api.finalizar_lote(
        lote_id,
        estado=estado,
        notas=notas,
    )

    print(
        "\nResumen:"
        f" equipos_ok={equipos_ok}/{len(equipos)},"
        f" jugadores={jugadores_total},"
        f" errores={len(errores)}"
    )

    if errores:
        raise RuntimeError(
            f"Plantillas terminaron con {len(errores)} error(es). "
            f"Primer error: {errores[0]}"
        )


if __name__ == "__main__":
    main()
