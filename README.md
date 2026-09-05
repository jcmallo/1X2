# Predictor 1X2

Sistema para pronosticar La Quiniela y proponer un boleto. Captura los datos,
entrena un modelo, calcula qué signos merecen la pena y publica todo en un
panel web.

**Qué esperar de esto.** No es un sistema que gane dinero. En el backtest
sobre 989 jornadas con premios reales, ninguna estrategia dio beneficio con
significancia estadística; la mejor perdió un 14,4% con un intervalo de
confianza del 90% entre −40% y +17%. Lo que hace es reducir la pérdida
esperada frente a jugar al favorito (−44,8%) o a seguir al público (−69,2%).
Los detalles están en [analisis/HALLAZGOS.md](analisis/HALLAZGOS.md).

## Cómo está montado

```
GitHub Actions  ──HTTP + X-Ingest-Token──▶  API PHP en IONOS  ──PDO──▶  MariaDB
   (Python)                                  /api/ingesta/              47 tablas
                                                    │
                                                    ▼
                                          Panel web (index.php)
```

**GitHub nunca se conecta a MariaDB.** Solo conoce dos secretos,
`INGEST_API_URL` e `INGEST_API_TOKEN`. Las credenciales de base de datos
viven únicamente en `_private/config.local.php` del servidor.

El panel tampoco usa el token: lee la base directamente con `db()`. Si
llamara a la API por HTTP tendría que llevar el token en el navegador, donde
cualquiera puede leerlo.

## Los módulos

| Carpeta | Qué hace |
|---|---|
| `ingestion/` | Calendarios, clima, estadios y plantillas. El cliente HTTP común (`api_client.py`) |
| `historico/` | Importa jornadas pasadas desde el buscador oficial de SELAE, con premios reales |
| `mercado/` | Porcentajes de LAE y cuotas de las casas |
| `modelado/` | El pronóstico propio: características, Elo y modelo de goles |
| `optimizador/` | Decide qué marcar en cada casilla y con qué presupuesto |
| `analisis/` | Backtesting sobre el histórico. **Aquí están los hallazgos que sostienen todo lo demás** |

## De dónde sale cada columna del panel

| Columna | Fuente | Qué significa |
|---|---|---|
| **% LAE** | loteriasyapuestas.es | Cuánta gente juega cada signo. Dice qué hace el público, no qué va a pasar |
| **% Apuestas** | BetFair y Matchbook (LaLiga, Segunda), 1xBet y SportyTrader (Liga F) | Probabilidad implícita en las cuotas, ya sin margen |
| **% Predictor** | `modelado/pronosticar.py` | Nuestro modelo |
| **Valor** | Apuestas ÷ LAE | Por encima de 1 el signo está infrajugado y paga más de lo que le toca |

El **valor** es la idea central. El premio se reparte entre acertantes, así
que un signo que acierta poca gente paga más. Medido sobre el histórico: la
columna menos jugada pagó una mediana de 5,00 EUR y la más jugada 1,10 EUR,
un factor de 4,5.

## Qué tan bueno es el modelo

Medido sobre partidos posteriores a los de entrenamiento, nunca al azar:

| Competición | Acierto | Mejora sobre el baseline |
|---|---|---|
| Liga F | 62,5% | +0,2344 |
| LaLiga | 51,4% | +0,0514 |
| Segunda División | 47,1% | +0,0030 |

El pronóstico solo se publica donde no empeora al baseline. En Liga F es la
única señal disponible, porque ninguna casa grande la cotizaba hasta que se
añadió 1xBet. En Segunda apenas aporta: es la liga más igualada que existe.

Para el Pleno al 15 hay un modelo aparte, de goles por equipo (0/1/2/M):
acierta la categoría de un equipo el 37,2% de las veces, con una mejora de
+0,0470 sobre el baseline.

## Datos

| | |
|---|---|
| Partidos con resultado | **4.402** — Segunda 1.882, LaLiga 1.552, Liga F 968 |
| Temporadas completas | 2022-23 a 2025-26, más la actual |
| Jornadas de quiniela | **686** desde 2016-17, con premios oficiales de SELAE |

## La regla que no se puede romper

Cada partido se describe **solo con lo anterior a él**. El estado de los
equipos se actualiza después de generar sus características, nunca antes. Y
los datos que cambian durante la semana —los porcentajes de LAE, las
cuotas— se guardan con su franja temporal (`T-72`, `T-24`, `T-2`, `CIERRE`),
para que al entrenar se use lo que estaba disponible antes de decidir y no
lo definitivo.

Saltarse esto da un modelo que parece excelente en las pruebas y fracasa en
cuanto se usa: en este proyecto ya ocurrió tres veces, y está documentado en
`analisis/HALLAZGOS.md` para no repetirlo.

## Los workflows

Están explicados uno a uno en [WORKFLOWS.md](WORKFLOWS.md), con el orden en
que deben correr y una tabla de «si esto falta en el panel, lanza esto».

## Secretos

```
INGEST_API_URL      https://1x2.juancarlosmallo.com/api/ingesta/
INGEST_API_TOKEN    el token privado
ODDS_API_KEY        para The Odds API (cuotas de LaLiga y Segunda)
```

Nunca en el repositorio: credenciales de MariaDB, cadenas de conexión ni
nada de IONOS.

## Lo que falta

- **Medir jornadas reales.** El backtest da un intervalo tan ancho que no
  distingue perder un 15% de ganar un 10%. Solo acumular jornadas decididas
  antes de conocer el resultado lo estrechará.
- **Rehacer el backtest sobre datos de SELAE.** Las cifras de HALLAZGOS.md
  salen de Quinielandia; ahora hay 686 jornadas con premios oficiales.
- **Valoración de los jugadores.** Hay 1.378 en la base pero ningún endpoint
  que los lea, así que no se sabe si el valor de mercado está guardado.
- **Prensa y alineaciones.** Su utilidad es dudosa donde el mercado ya está
  calibrado; tendría sentido en Liga F, que nadie cotiza con detalle.
