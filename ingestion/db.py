"""
Conexión a la base de datos MariaDB (IONOS).

Las credenciales NUNCA se escriben en este archivo. Se leen de variables
de entorno, que en GitHub Actions vienen de "Secrets" del repositorio
(Settings -> Secrets and variables -> Actions):

    DB_HOST      -> db5021337172.hosting-data.io
    DB_PORT      -> 3306
    DB_USER      -> dbu1295308
    DB_PASSWORD  -> (la contraseña real, nunca en el código)
    DB_NAME      -> dbs16085248

Para probar en tu propio ordenador, exporta esas variables antes de
ejecutar el script (o usa un archivo .env con python-dotenv), pero
JAMÁS subas ese archivo con la contraseña a GitHub.
"""

import os
import pymysql
import pymysql.cursors


def obtener_conexion():
    """Abre una conexión a la base de datos y la devuelve.

    Lanza una excepción clara si falta alguna variable de entorno,
    en vez de fallar con un error críptico de conexión.
    """
    requeridas = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]
    faltantes = [v for v in requeridas if os.environ.get(v) is None]
    if faltantes:
        raise RuntimeError(
            f"Faltan variables de entorno: {', '.join(faltantes)}. "
            "Defínelas como Secrets en GitHub Actions o expórtalas localmente."
        )

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,   # controlamos el commit a mano para poder hacer rollback si algo falla a medias
    )
