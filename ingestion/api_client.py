"""
Cliente HTTP/HTTPS para el puente PHP alojado en IONOS.

GitHub Actions no se conecta directamente a MariaDB. Solo conoce:
    INGEST_API_URL
    INGEST_API_TOKEN
"""

from __future__ import annotations

import os
import time
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

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        timeout: int = 30,
    ) -> dict:
        """Petición al puente IONOS con reintentos ante fallos temporales."""
        ultimo_error: Exception | None = None

        for intento in range(1, 4):
            try:
                r = self.session.request(
                    method,
                    self._url(endpoint),
                    params=params,
                    json=json,
                    timeout=timeout,
                )

                if r.status_code in {429, 500, 502, 503, 504} and intento < 3:
                    print(
                        f"API IONOS HTTP {r.status_code}; "
                        f"reintento {intento}/3..."
                    )
                    time.sleep(2 ** (intento - 1))
                    continue

                return self._json_o_error(r)

            except (requests.Timeout, requests.ConnectionError) as exc:
                ultimo_error = exc
                if intento < 3:
                    print(
                        "API IONOS temporalmente no disponible; "
                        f"reintento {intento}/3..."
                    )
                    time.sleep(2 ** (intento - 1))
                    continue
                raise

        raise RuntimeError(
            f"No se pudo completar la petición a IONOS: {ultimo_error}"
        )

    def health(self) -> dict:
        return self._request_json("GET", "health.php", timeout=20)

    def contexto_clima(self, modo: str = "estadios", dias: int = 7) -> list[dict]:
        params = {"modo": modo}
        if modo == "proximos":
            params["dias"] = max(1, min(14, int(dias)))

        data = self._request_json(
            "GET",
            "contexto_clima.php",
            params=params,
            timeout=30,
        )
        items = data.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("La API devolvió 'items' con formato inválido.")
        return items

    def contexto_clima_historico(self, limite: int = 500) -> list[dict]:
        data = self._request_json(
            "GET",
            "contexto_clima_historico.php",
            params={"limite": max(1, min(1000, int(limite)))},
            timeout=45,
        )
        items = data.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError(
                "La API devolvió 'items' de clima histórico "
                "con formato inválido."
            )
        return items

    def guardar_clima_historico(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_clima_historico.php",
            json=payload,
            timeout=60,
        )

    def contexto_plantillas(
        self,
        temporada: str = "2026-27",
    ) -> list[dict]:
        data = self._request_json(
            "GET",
            "contexto_plantillas.php",
            params={"temporada": temporada},
            timeout=45,
        )
        items = data.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError(
                "La API devolvió 'items' de plantillas "
                "con formato inválido."
            )
        return items

    def guardar_plantilla(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_plantilla.php",
            json=payload,
            timeout=90,
        )

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
        data = self._request_json(
            "GET",
            "contexto_partidos.php",
            params=params,
            timeout=30,
        )
        jornadas = data.get("jornadas")
        if not isinstance(jornadas, list):
            raise RuntimeError(
                "La API devolvió 'jornadas' con formato inválido."
            )
        return data

    def contexto_estadios(
        self,
        *,
        solo_pendientes: bool = True,
        limite: int = 100,
    ) -> list[dict]:
        data = self._request_json(
            "GET",
            "contexto_estadios.php",
            params={
                "solo_pendientes": "1" if solo_pendientes else "0",
                "limite": max(1, min(200, int(limite))),
            },
            timeout=30,
        )
        items = data.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError(
                "La API devolvió 'items' de estadios con formato inválido."
            )
        return items

    def guardar_estadio(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_estadio.php",
            json=payload,
            timeout=45,
        )

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

        data = self._request_json(
            "POST",
            "iniciar_lote.php",
            json=payload,
            timeout=30,
        )
        return int(data["lote_id"])

    def guardar_clima(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_clima.php",
            json=payload,
            timeout=45,
        )

    def guardar_documento(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_documento.php",
            json=payload,
            timeout=60,
        )

    def guardar_partido(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_partido.php",
            json=payload,
            timeout=60,
        )

    def contexto_historico_estado(
        self,
        temporada: str,
    ) -> dict[str, dict]:
        data = self._request_json(
            "GET",
            "contexto_historico_estado.php",
            params={"temporada": temporada},
            timeout=60,
        )
        items = data.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError(
                "La API devolvió 'items' histórico con formato inválido."
            )
        salida: dict[str, dict] = {}
        for item in items:
            if isinstance(item, dict) and item.get("id_partido_fuente") is not None:
                salida[str(item["id_partido_fuente"])] = item
        return salida

    def guardar_partido_historico(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_partido_historico.php",
            json=payload,
            timeout=90,
        )

    def guardar_detalle_historico(self, payload: dict) -> dict:
        return self._request_json(
            "POST",
            "guardar_detalle_historico.php",
            json=payload,
            timeout=120,
        )

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

        return self._request_json(
            "POST",
            "finalizar_lote.php",
            json=payload,
            timeout=30,
        )
