#!/usr/bin/env python3
"""
Backfill de competiciones masculinas complementarias - v2.

CAMBIO CLAVE: Transfermarkt queda eliminado del proceso. No se usa HTML con
JavaScript/anti-bot ni búsqueda de IDs externos.

Cobertura validada en esta v2: 2022-23, 2023-24 y 2024-25.
- Copa del Rey: OpenFootball (raw.githubusercontent.com)
- Champions: OpenFootball
- Europa League: OpenFootball
- Conference League: OpenFootball
- Supercopa: catálogo pequeño de partidos verificados

Un partido solo se guarda si al menos uno de los clubes se resuelve contra los
equipos masculinos YA existentes en LaLiga/Segunda de esa temporada. Nunca se
crea un rival externo.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import requests

from api_client import ApiIngesta

TEMPORADAS_OK = ("2022-23", "2023-24", "2024-25")
COMPETICIONES = {
    "copa": "Copa del Rey",
    "supercopa": "Supercopa de España",
    "champions": "UEFA Champions League",
    "europa": "UEFA Europa League",
    "conference": "UEFA Conference League",
}
URLS_OPENFOOTBALL = {
    "copa": "https://raw.githubusercontent.com/openfootball/espana/master/{temporada}/cup.txt",
    "champions": "https://raw.githubusercontent.com/openfootball/champions-league/master/{temporada}/cl.txt",
    "europa": "https://raw.githubusercontent.com/openfootball/champions-league/master/{temporada}/el.txt",
    "conference": "https://raw.githubusercontent.com/openfootball/champions-league/master/{temporada}/conf.txt",
}
MESES = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
ALIAS_A_CANONICO = {
    "albacete": "albacete bp",
    "albacete balompie": "albacete bp",
    "athletic bilbao": "athletic club",
    "atletico madrid": "atletico de madrid",
    "cd alaves": "deportivo alaves",
    "deportivo la coruna": "rc deportivo",
    "espanyol barcelona": "rcd espanyol de barcelona",
    "rcd espanyol": "rcd espanyol de barcelona",
    "racing santander": "r racing club",
    "real racing club": "r racing club",
    "rc celta": "celta",
    "celta vigo": "celta",
    "sporting gijon": "real sporting",
    "real valladolid": "real valladolid cf",
    "villarreal cf b": "villarreal b",
    "villarreal b": "villarreal b",
}
SUPERCOPA = {
    "2022-23": [
        ("2023-01-11 20:00:00", "Semifinal", "Real Madrid", "Valencia CF", 1, 1, True, True),
        ("2023-01-12 20:00:00", "Semifinal", "Real Betis", "FC Barcelona", 2, 2, True, True),
        ("2023-01-15 20:00:00", "Final", "Real Madrid", "FC Barcelona", 1, 3, False, False),
    ],
    "2023-24": [
        ("2024-01-10 20:00:00", "Semifinal", "Real Madrid", "Atlético de Madrid", 5, 3, True, False),
        ("2024-01-11 20:00:00", "Semifinal", "FC Barcelona", "CA Osasuna", 2, 0, False, False),
        ("2024-01-14 20:00:00", "Final", "Real Madrid", "FC Barcelona", 4, 1, False, False),
    ],
    "2024-25": [
        ("2025-01-08 20:00:00", "Semifinal", "Athletic Club", "FC Barcelona", 0, 2, False, False),
        ("2025-01-09 20:00:00", "Semifinal", "Real Madrid", "RCD Mallorca", 3, 0, False, False),
        ("2025-01-12 20:00:00", "Final", "Real Madrid", "FC Barcelona", 2, 5, False, False),
    ],
}
DATE_LINE_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2})(?:\s+(\d{4}))?$"
)
TIME_PREFIX_RE = re.compile(r"^(\d{1,2}:\d{2})\s+(.*)$")
COUNTRY_SUFFIX_RE = re.compile(r"\s+\([A-Z]{3}\)\s*$")
SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)")

@dataclass(frozen=True)
class Equipo:
    equipo_id: int
    nombre: str

@dataclass
class Partido:
    fuente: str
    id_fuente: str
    competicion: str
    ronda: str | None
    fecha_sql: str
    local: str
    visitante: str
    goles_local: int | None
    goles_visitante: int | None
    hubo_prorroga: bool
    hubo_penaltis: bool
    resultado_raw: str | None
    url: str
    equipo_local_id: int | None = None
    equipo_visitante_id: int | None = None

def norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower().replace("&", " and ")
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()

def quitar_pais(nombre: str) -> str:
    return COUNTRY_SUFFIX_RE.sub("", nombre).strip()

def canonical_key(nombre: str) -> str:
    n = norm(quitar_pais(nombre))
    return ALIAS_A_CANONICO.get(n, n)

def indice_equipos(equipos: Iterable[Equipo]) -> dict[str, Equipo]:
    idx: dict[str, Equipo] = {}
    for e in equipos:
        k = canonical_key(e.nombre)
        if k in idx and idx[k].equipo_id != e.equipo_id:
            raise RuntimeError(f"Colisión de equipos al normalizar: {e.nombre}")
        idx[k] = e
    return idx

def resolver_equipo(nombre_fuente: str, idx: dict[str, Equipo]) -> Equipo | None:
    return idx.get(canonical_key(nombre_fuente))

def descargar_texto(url: str, timeout: int = 45) -> str:
    headers = {
        "User-Agent": "quiniela-1x2-github-actions/2.0",
        "Accept": "text/plain,*/*;q=0.8",
    }
    ultimo = None
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            texto = r.text
            if len(texto) < 200 or not texto.lstrip().startswith("="):
                raise RuntimeError(
                    f"respuesta inesperada: {len(texto)} bytes; inicio={texto[:80]!r}"
                )
            return texto
        except Exception as exc:
            ultimo = exc
    raise RuntimeError(f"No pude descargar fuente estática {url}: {ultimo}")

def fecha_de_linea(linea: str, temporada: str) -> str | None:
    m = DATE_LINE_RE.match(linea.strip())
    if not m:
        return None
    mes_txt, dia_txt, anyo_txt = m.groups()
    mes = MESES[mes_txt]
    dia = int(dia_txt)
    if anyo_txt:
        anyo = int(anyo_txt)
    else:
        inicio = int(temporada[:4])
        anyo = inicio if mes >= 7 else inicio + 1
    return f"{anyo:04d}-{mes:02d}-{dia:02d}"

def resultado_principal(resultado: str) -> tuple[int | None, int | None, bool, bool]:
    pen = "pen." in resultado.lower()
    aet = "a.e.t." in resultado.lower()
    scores = SCORE_RE.findall(resultado)
    if not scores:
        return None, None, aet, pen
    gl, gv = scores[1] if pen and len(scores) >= 2 else scores[0]
    return int(gl), int(gv), aet, pen

def separar_copa(cuerpo: str) -> tuple[str, str, str] | None:
    matches = list(SCORE_RE.finditer(cuerpo))
    if not matches:
        return None
    inicio = matches[0].start()
    local = cuerpo[:inicio].strip()
    resto = cuerpo[inicio:].strip()
    patron = re.compile(
        r"^((?:\d{1,2}-\d{1,2}\s+pen\.\s+)?"
        r"\d{1,2}-\d{1,2}(?:\s+a\.e\.t\.)?"
        r"(?:\s+\([^)]*\))?)\s+(.+)$"
    )
    m = patron.match(resto)
    if not m:
        return None
    resultado, visitante = m.groups()
    return local, visitante.strip(), resultado.strip()

def separar_uefa(cuerpo: str) -> tuple[str, str, str] | None:
    partes = re.split(r"\s+v\s+", cuerpo, maxsplit=1)
    if len(partes) != 2:
        return None
    local = partes[0].strip()
    der = partes[1].strip()
    matches = list(SCORE_RE.finditer(der))
    if not matches:
        return None
    inicio = matches[0].start()
    visitante = der[:inicio].strip()
    resultado = der[inicio:].strip()
    return quitar_pais(local), quitar_pais(visitante), resultado

def parse_openfootball(texto: str, *, temporada: str, competicion: str, url: str,
                       idx: dict[str, Equipo]) -> list[Partido]:
    fecha: str | None = None
    hora: str | None = None
    ronda: str | None = None
    out: list[Partido] = []
    for raw in texto.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("="):
            continue
        if stripped.startswith("▪"):
            ronda = stripped.lstrip("▪").strip() or None
            continue
        f = fecha_de_linea(stripped, temporada)
        if f:
            fecha = f
            hora = None
            continue
        if fecha is None:
            continue
        cuerpo = stripped
        mt = TIME_PREFIX_RE.match(cuerpo)
        if mt:
            hora = mt.group(1)
            cuerpo = mt.group(2).strip()
        if hora is None:
            continue
        sep = separar_copa(cuerpo) if competicion == "Copa del Rey" else separar_uefa(cuerpo)
        if not sep:
            continue
        local, visitante, resultado = sep
        gl, gv, prorroga, penaltis = resultado_principal(resultado)
        if gl is None or gv is None:
            continue
        local_db = resolver_equipo(local, idx)
        visitante_db = resolver_equipo(visitante, idx)
        if local_db is None and visitante_db is None:
            continue
        fecha_sql = f"{fecha} {hora}:00"
        semilla = "|".join(["openfootball", temporada, competicion, fecha_sql, norm(local), norm(visitante)])
        out.append(Partido(
            fuente="openfootball",
            id_fuente=hashlib.sha1(semilla.encode("utf-8")).hexdigest(),
            competicion=competicion,
            ronda=ronda,
            fecha_sql=fecha_sql,
            local=local,
            visitante=visitante,
            goles_local=gl,
            goles_visitante=gv,
            hubo_prorroga=prorroga,
            hubo_penaltis=penaltis,
            resultado_raw=resultado,
            url=url,
            equipo_local_id=local_db.equipo_id if local_db else None,
            equipo_visitante_id=visitante_db.equipo_id if visitante_db else None,
        ))
    if not out:
        raise RuntimeError(
            f"Parser OpenFootball devolvió 0 partidos relevantes para {competicion} {temporada}. "
            "No se escribirá nada."
        )
    return out

def partidos_supercopa(temporada: str, idx: dict[str, Equipo]) -> list[Partido]:
    out: list[Partido] = []
    for fecha, ronda, local, visitante, gl, gv, pro, pen in SUPERCOPA.get(temporada, []):
        ldb = resolver_equipo(local, idx)
        vdb = resolver_equipo(visitante, idx)
        if ldb is None and vdb is None:
            continue
        semilla = "|".join(["supercopa-verificada", temporada, fecha, norm(local), norm(visitante)])
        out.append(Partido(
            fuente="supercopa-verificada",
            id_fuente=hashlib.sha1(semilla.encode("utf-8")).hexdigest(),
            competicion="Supercopa de España",
            ronda=ronda,
            fecha_sql=fecha,
            local=local,
            visitante=visitante,
            goles_local=gl,
            goles_visitante=gv,
            hubo_prorroga=pro,
            hubo_penaltis=pen,
            resultado_raw=f"{gl}-{gv}",
            url="https://www.rfef.es/competiciones/supercopa-de-espana",
            equipo_local_id=ldb.equipo_id if ldb else None,
            equipo_visitante_id=vdb.equipo_id if vdb else None,
        ))
    return out

def cargar_contexto(api: ApiIngesta, temporada: str) -> list[Equipo]:
    data = api._request_json(
        "GET", "contexto_equipos_complementarios.php",
        params={"temporada": temporada}, timeout=45,
    )
    items = data.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError("Contexto de equipos con formato inválido.")
    return [Equipo(int(x["equipo_id"]), str(x["nombre_canonico"])) for x in items]

def seleccion_claves(arg: str) -> list[str]:
    return ["copa", "supercopa", "champions", "europa", "conference"] if arg == "todas" else [arg]

def ahora_sql() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--temporada", required=True,
                    choices=("2022-23", "2023-24", "2024-25", "2025-26", "2026-27"))
    ap.add_argument("--competicion", default="todas",
                    choices=("todas", "copa", "supercopa", "champions", "europa", "conference"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.temporada not in TEMPORADAS_OK:
        raise RuntimeError(
            f"La v2 está validada para {', '.join(TEMPORADAS_OK)}. "
            f"No ejecuto {args.temporada} para no introducir otra fuente sin validar."
        )
    api = ApiIngesta()
    health = api.health()
    print("Puente IONOS OK ->", health.get("database"), health.get("db_version"))
    equipos = cargar_contexto(api, args.temporada)
    if not equipos:
        raise RuntimeError("IONOS devolvió 0 equipos seguidos.")
    idx = indice_equipos(equipos)
    print(f"Temporada {args.temporada}: {len(equipos)} equipos ya existentes.")
    print("Transfermarkt: DESACTIVADO")
    partidos: list[Partido] = []
    raws: list[tuple[str, str, str]] = []
    for clave in seleccion_claves(args.competicion):
        if clave == "supercopa":
            ps = partidos_supercopa(args.temporada, idx)
            print(f"Supercopa: {len(ps)} partidos relevantes")
            partidos.extend(ps)
            continue
        url = URLS_OPENFOOTBALL[clave].format(temporada=args.temporada)
        comp = COMPETICIONES[clave]
        print(f"Descargando {comp}: {url}")
        texto = descargar_texto(url)
        cabecera = texto.splitlines()[0].strip() if texto.splitlines() else ""
        print(f"  fuente OK: {cabecera} ({len(texto)} bytes)")
        ps = parse_openfootball(texto, temporada=args.temporada, competicion=comp, url=url, idx=idx)
        print(f"  partidos de nuestros equipos: {len(ps)}")
        partidos.extend(ps)
        raws.append(("openfootball", url, texto))
    unicos: dict[tuple[str, str], Partido] = {}
    for p in partidos:
        unicos[(p.fuente, p.id_fuente)] = p
    partidos = list(unicos.values())
    if args.dry_run:
        print("\nDRY-RUN OK: no se escribió nada en IONOS.")
        por_comp: dict[str, int] = {}
        for p in partidos:
            por_comp[p.competicion] = por_comp.get(p.competicion, 0) + 1
        for comp, n in sorted(por_comp.items()):
            print(f"  {comp}: {n}")
        print(f"TOTAL relevantes: {len(partidos)}")
        return
    lote = api.iniciar_lote(
        fuente="openfootball", tipo_fuente="cal_extra",
        notas=f"Competiciones masculinas v2 {args.temporada}; seleccion={args.competicion}; sin Transfermarkt",
    )
    print("Lote abierto:", lote)
    for fuente, url, texto in raws:
        api.guardar_documento({
            "lote_id": lote, "fuente": fuente, "url": url,
            "tipo_contenido": "openfootball_txt", "obtenido_en": ahora_sql(), "contenido": texto,
        })
    creados = actualizados = errores = 0
    mensajes: list[str] = []
    for p in sorted(partidos, key=lambda x: (x.fecha_sql, x.competicion, x.local)):
        payload = {
            "lote_id": lote, "fuente": p.fuente, "id_partido_fuente": p.id_fuente,
            "temporada": args.temporada, "competicion": p.competicion, "ronda": p.ronda,
            "es_clasificatoria": False, "fecha_hora_inicio": p.fecha_sql,
            "hora_confirmada": True, "estado": "FINALIZADO",
            "equipo_local_id": p.equipo_local_id, "equipo_visitante_id": p.equipo_visitante_id,
            "local_nombre": p.local, "visitante_nombre": p.visitante,
            "local_id_fuente": None, "visitante_id_fuente": None,
            "goles_local": p.goles_local, "goles_visitante": p.goles_visitante,
            "hubo_prorroga": p.hubo_prorroga, "hubo_penaltis": p.hubo_penaltis,
            "resultado_raw": p.resultado_raw, "url": p.url, "obtenido_en": ahora_sql(),
        }
        try:
            res = api._request_json("POST", "guardar_partido_complementario.php", json=payload, timeout=60)
            if res.get("accion") == "creado": creados += 1
            else: actualizados += 1
            print(f"{p.fecha_sql[:16]} [{p.competicion}] {p.local} {p.goles_local}-{p.goles_visitante} {p.visitante} -> {res.get('accion')}")
        except Exception as exc:
            errores += 1
            msg = f"{p.competicion} {p.local}-{p.visitante}: {exc}"
            mensajes.append(msg)
            print("ERROR:", msg)
    api.finalizar_lote(
        lote, estado="completado" if errores == 0 else "error",
        notas=f"v2; partidos={len(partidos)}; creados={creados}; actualizados={actualizados}; errores={errores}",
    )
    print(f"\nRESUMEN {args.temporada}: creados={creados}, actualizados={actualizados}, errores={errores}, total={len(partidos)}")
    if mensajes:
        for m in mensajes: print(" -", m)
        raise RuntimeError(f"Backfill terminó con {errores} errores.")

if __name__ == "__main__":
    main()
