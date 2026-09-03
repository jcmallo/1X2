"""
Cliente HTTP/HTTPS para el puente PHP alojado en IONOS.

GitHub Actions no se conecta directamente a MariaDB. Solo conoce:
    INGEST_API_URL
    INGEST_API_TOKEN
"""

from __future__ import annotations

import os
from urllib.parse import urljoin

import requests


class ApiIngesta:
    def __init__(self) -> None:
        base_url = os.environ.get("INGEST_API_URL", "").strip().rstrip("/")
        token = os.environ.get("INGEST_API_TOKEN", "").strip()

        faltantes = []
        if not base_url:
            faltantes.append("INGEST_API_URL")
        if not token:
            faltantes.append("INGEST_API_TOKEN")
        if faltantes:
            raise RuntimeError(
                "Faltan variables de entorno: " + ", ".join(faltantes)
            )

        if not base_url.startswith(("http://", "https://")):
            raise RuntimeError(
                "INGEST_API_URL debe empezar por http:// o https://"
            )

        if base_url.startswith("http://"):
            print(
                "AVISO: INGEST_API_URL usa HTTP sin cifrar. "
                "Activa SSL/HTTPS en IONOS cuanto antes porque el token "
                "viaja por la red."
            )

        self.base_url = base_url + "/"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Ingest-Token": token,
                "Accept": "application/json",
                "User-Agent": "quiniela-1x2-github-actions/1.1",
            }
        )

    def _url(self, endpoint: str) -> str:
        return urljoin(self.base_url, endpoint.lstrip("/"))

    @staticmethod
    def _json_o_error(respuesta: requests.Response) -> dict:
        try:
            data = respuesta.json()
        except ValueError as exc:
            texto = respuesta.text[:500]
            raise RuntimeError(
                f"La API devolvió una respuesta no JSON "
                f"(HTTP {respuesta.status_code}): {texto}"
            ) from exc

        if not respuesta.ok:
            mensaje = data.get("error") if isinstance(data, dict) else None
            raise RuntimeError(
                f"API IONOS HTTP {respuesta.status_code}: "
                f"{mensaje or data}"
            )

        if not isinstance(data, dict) or data.get("ok") is not True:
            raise RuntimeError(f"Respuesta inesperada de API IONOS: {data}")

        return data

    def health(self) -> dict:
        r = self.session.get(self._url("health.php"), timeout=20)
        return self._json_o_error(r)

    def contexto_clima(self, modo: str = "estadios", dias: int = 7) -> list[dict]:
        params = {"modo": modo}
        if modo == "proximos":
            params["dias"] = max(1, min(14, int(dias)))

        r = self.session.get(
            self._url("contexto_clima.php"),
            params=params,
            timeout=30,
        )
        data = self._json_o_error(r)
        items = data.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("La API devolvió 'items' con formato inválido.")
        return items

    def contexto_partidos(
        self,
        competicion: str,
        temporada: str,
        genero: str,
        *,
        retro_horas: int = 72,
        futuro_dias: int = 21,
        adelante_jornadas: int = 3,
        reconciliar: bool = False,
        ultimas_jornadas: int = 4,
    ) -> dict:
        params = {
            "competicion": competicion,
            "temporada": temporada,
            "genero": genero,
            "retro_horas": max(12, min(336, int(retro_horas))),
            "futuro_dias": max(3, min(60, int(futuro_dias))),
            "adelante_jornadas": max(1, min(6, int(adelante_jornadas))),
            "reconciliar": "1" if reconciliar else "0",
            "ultimas_jornadas": max(1, min(10, int(ultimas_jornadas))),
        }
        r = self.session.get(
            self._url("contexto_partidos.php"),
            params=params,
            timeout=30,
        )
        data = self._json_o_error(r)
        jornadas = data.get("jornadas")
        if not isinstance(jornadas, list):
            raise RuntimeError(
                "La API devolvió 'jornadas' con formato inválido."
            )
        return data

    def iniciar_lote(
        self,
        fuente: str = "open-meteo",
        tipo_fuente: str = "api",
        notas: str | None = None,
    ) -> int:
        payload = {
            "fuente": fuente,
            "tipo_fuente": tipo_fuente,
        }
        if notas:
            payload["notas"] = notas

        r = self.session.post(
            self._url("iniciar_lote.php"),
            json=payload,
            timeout=30,
        )
        data = self._json_o_error(r)
        return int(data["lote_id"])

    def guardar_clima(self, payload: dict) -> dict:
        r = self.session.post(
            self._url("guardar_clima.php"),
            json=payload,
            timeout=45,
        )
        return self._json_o_error(r)

    def guardar_documento(self, payload: dict) -> dict:
        r = self.session.post(
            self._url("guardar_documento.php"),
            json=payload,
            timeout=60,
        )
        return self._json_o_error(r)

    def guardar_partido(self, payload: dict) -> dict:
        r = self.session.post(
            self._url("guardar_partido.php"),
            json=payload,
            timeout=60,
        )
        return self._json_o_error(r)

    def finalizar_lote(
        self,
        lote_id: int,
        estado: str = "completado",
        notas: str | None = None,
    ) -> dict:
        payload = {
            "lote_id": int(lote_id),
            "estado": estado,
        }
        if notas:
            payload["notas"] = notas

        r = self.session.post(
            self._url("finalizar_lote.php"),
            json=payload,
            timeout=30,
        )
        return self._json_o_error(r)
