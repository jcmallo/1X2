# Qué hace cada workflow

Los nombres se parecían demasiado y costaba saber cuál lanzar. Esta es la
lista, por orden de la semana.

## Cada jornada, automáticos

| Workflow | Cuándo | Para qué |
|---|---|---|
| Vincular boleto con los partidos | mié y vie 10:40 | Une cada casilla con su partido de la base. Sin esto no hay ni horario ni cuotas |
| Capturar porcentajes de LAE | mié, jue, vie 11:00 · sáb 12:00 | Qué juega la gente (loteriasyapuestas.es) |
| Capturar cuotas | mié, jue, vie 11:00 | Cuotas de LaLiga y Segunda (The Odds API) |
| Capturar cuotas de Liga F (1xBet) | mié, jue, vie 11:10 · sáb 12:10 | Cuotas de Liga F: los ocho partidos |
| Capturar Pleno al 15 (marcador exacto) | mié, jue, vie 11:10 · sáb 12:10 | Los goles 0/1/2/M del partido 15, del marcador exacto |
| Pronosticar con modelo propio | vie 11:15 · sáb 12:15 | Entrena y guarda el % Predictor |
| Proponer boleto | vie 11:30 · sáb 12:30 | Calcula la propuesta y la guarda |

El orden importa: vincular antes de capturar, capturar antes de pronosticar,
pronosticar antes de proponer.

## A mano, cuando hagan falta

| Workflow | Para qué |
|---|---|
| Importar histórico completo | Trae todas las jornadas desde 2016-17 de una vez |
| Importar jornadas pasadas de SELAE | Lo mismo pero por temporadas sueltas |

## Si algo no sale en el panel

- **Falta el horario de una casilla** → Vincular boleto con los partidos
- **Falta % LAE** → Capturar porcentajes de LAE
- **Falta % Apuestas en Liga F** → Capturar cuotas de Liga F (1xBet)
- **Falta % Apuestas en LaLiga o Segunda** → Capturar cuotas
- **Falta % Predictor** → Pronosticar con modelo propio
- **El Pleno no propone goles** → Capturar Pleno al 15
