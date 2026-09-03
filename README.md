# Quiniela IA — Ingesta de datos (Python + GitHub Actions)

Este repositorio contiene los scripts que alimentan la base de datos
(`dbs16085248` en IONOS) de forma programada, sin necesidad de ningún
servidor Python encendido todo el día. GitHub Actions ejecuta el script,
escribe en la base de datos, y termina. El PHP del subdominio
`1x2.juancarlosmallo.com` solo lee esa base de datos.

## Estructura

```
.
├── ingestion/
│   ├── db.py                  # conexión a MariaDB (lee credenciales de variables de entorno)
│   ├── clima_open_meteo.py    # primer ingestor: previsión meteorológica (no necesita API key)
│   └── test_local_clima.py    # prueba local con datos simulados, para desarrollar sin gastar llamadas reales
├── requirements.txt
└── .github/workflows/
    └── actualizar_clima.yml   # ejecuta clima_open_meteo.py cada día a las 06:00 UTC, y también a mano
```

## Configuración obligatoria antes de usarlo (una sola vez)

En GitHub: entra al repositorio → **Settings** → **Secrets and variables** →
**Actions** → **New repository secret**, y crea estos 5 secretos (los
valores son los que ya tienes de IONOS):

| Nombre | Valor |
|---|---|
| `DB_HOST` | `db5021337172.hosting-data.io` |
| `DB_PORT` | `3306` |
| `DB_USER` | `dbu1295308` |
| `DB_PASSWORD` | tu contraseña real |
| `DB_NAME` | `dbs16085248` |

Los "Secrets" de GitHub están cifrados, no se ven en los logs y nadie
con acceso de lectura al repo puede leerlos — es el sitio correcto para
esto, nunca los escribas directamente en un archivo `.py`.

**Importante:** el firewall de la base de datos en IONOS puede estar
limitando qué IPs se pueden conectar. Si el workflow falla con un error
de conexión (timeout), entra al panel de IONOS → tu base de datos →
"Acceso externo" / "Direcciones IP permitidas" y comprueba que permite
conexiones desde fuera (GitHub Actions usa IPs dinámicas, así que
normalmente hay que poner "permitir todas" o un rango amplio — es un
paso habitual la primera vez que se conecta algo externo a una base de
datos de hosting compartido).

## Cómo probarlo tú mismo antes de confiar en el automatismo

1. La primera vez, ejecútalo a mano desde GitHub: pestaña **Actions** →
   "Actualizar clima" → botón **Run workflow**.
2. Revisa el log de la ejecución (te dice cuántos estadios procesó).
3. Comprueba en tu base de datos (phpMyAdmin) que aparecieron filas
   nuevas en `bruto_lotes_ingesta`, `bruto_respuestas_api` y
   `clima_previsiones`.

## Próximos ingestores (siguiente iteración)

Este primer script cubre solo el clima porque no necesita ninguna
clave de API. El siguiente paso natural es un ingestor de partidos
(calendario/resultados) contra Sportmonks o API-Football, que sí
requiere que te crees una cuenta gratuita y generes una API key —
esa clave se guardaría como un Secret más (`SPORTMONKS_API_KEY` o
similar), siguiendo exactamente el mismo patrón que `DB_PASSWORD`.
