"""
Prueba mínima del puente GitHub -> PHP IONOS -> MariaDB.
No escribe nada en la base de datos.
"""

from api_client import ApiIngesta


def main():
    api = ApiIngesta()
    data = api.health()

    print("Puente IONOS correcto.")
    print("Base de datos:", data.get("database"))
    print("MariaDB:", data.get("db_version"))
    print("PHP:", data.get("php"))


if __name__ == "__main__":
    main()
