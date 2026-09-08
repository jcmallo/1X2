# El panel

Lo que se sirve desde IONOS, en `1x2.juancarlosmallo.com`.

Está aquí para poder versionarlo y trabajarlo como el resto del proyecto.
GitHub no despliega nada: la subida a IONOS sigue siendo manual.

## Lo que NO puede subir aquí

El repositorio es **público**. Estos archivos llevan credenciales y solo
deben existir en el servidor:

| Archivo | Qué contiene |
|---|---|
| `api/ingesta/bootstrap.php` | usuario y contraseña de MariaDB |
| `config_ia.php` | token de GitHub y clave del botón de la IA |

El `.gitignore` de esta carpeta los excluye, pero **la web de GitHub no
aplica el .gitignore** cuando se arrastran archivos: ahí hay que dejarlos
fuera a mano.

Si alguno acabara subido por accidente, no basta con borrarlo en un commit
posterior — queda en el historial. Habría que cambiar la contraseña de la
base y regenerar el token.

## Qué hay aquí

    index.php          el panel
    optimizador.php    puerto PHP de optimizador/cartera.py
    api/ingesta/       los endpoints que llama GitHub Actions
    imagenes/          logo e icono

## El puerto PHP

`optimizador.php` es una traducción de `optimizador/cartera.py`. Los dos
tienen que decir lo mismo, o el panel y GitHub propondrían boletos
distintos. Cualquier cambio en uno va en el otro, y se comprueba
ejecutando las dos versiones sobre las mismas ternas.
