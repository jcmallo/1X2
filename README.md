# Quiniela 1X2 - Sistema de Ingesta de Datos

Ingestión automatizada de datos para predicción de resultados de la Quiniela 1X2. Recopila datos históricos y actuales de ligas españolas, competiciones complementarias, clima y estadios.

## Arquitectura

```
┌─────────────────────┐
│  GitHub Actions     │
│  Python Ingestors   │
└──────────┬──────────┘
           │ HTTP/HTTPS JSON
           │ X-Ingest-Token (autenticación)
           ▼
┌─────────────────────┐
│  IONOS API PHP      │
│  /api/ingesta/      │
│  (20 endpoints)     │
└──────────┬──────────┘
           │ PDO local
           ▼
┌─────────────────────┐
│  MariaDB IONOS      │
│  dbs16085248        │
│  (47 tablas)        │
└─────────────────────┘
```

## Principios Clave

**GitHub NO se conecta directamente a MariaDB.** Esto garantiza:
- ✅ GitHub Actions no tiene credenciales de base de datos
- ✅ Seguridad: solo token API en secretos
- ✅ Escalabilidad: API puede reemplazarse o cacharse
- ✅ Trazabilidad: todas las peticiones se registran en `bruto_respuestas_api`

## Secretos Requeridos en GitHub Actions

Solo necesitas **dos** variables de entorno:

```
INGEST_API_URL      → https://1x2.juancarlosmallo.com/api/ingesta/
INGEST_API_TOKEN    → token privado de seguridad
```

**NO** guardes en GitHub:
- `DB_HOST`, `DB_USER`, `DB_PASSWORD`
- Cadenas de conexión MariaDB
- Credenciales IONOS

## Scripts de Ingesta Activos

| Script | Propósito | Fuente | Frecuencia | Modo |
|--------|-----------|--------|-----------|------|
| `laliga_partidos.py` | Primera/Segunda 2026-27 | laliga.com | Diaria | Incremental |
| `ligaf_partidos.py` | Liga F 2026-27 | ligaf.es | Diaria | Incremental |
| `competiciones_masculinas.py` | Copa, Champions, Europa, Conference | OpenFootball, FixtureDownload | Semanal | Update |
| `competiciones_femeninas.py` | Copa Reina, Supercopa Fem., UWCL | SoccerDonna | Semanal | Update |
| `clima_open_meteo.py` | Clima actual/futuro | Open-Meteo | Diaria | Overwrite |
| `plantillas_2026_27.py` | Plantillas de equipos | laliga.com, ligaf.es | Semanal | Overwrite |
| `estadios_geocodificar.py` | Geocodificación de nuevos estadios | OpenStreetMap Nominatim | On-demand | Idempotente |

## Datasets Disponibles

### Histórico (Completado)

- **Primera División:** 2022-23 a 2025-26 (380 × 5 = 1,900 partidos)
- **Segunda División:** 2022-23 a 2025-26 (462 × 5 = 2,310 partidos)
- **Liga F:** 2022-23 a 2025-26 (480 × 5 = 2,400 partidos femeninos)
- **Competiciones Complementarias:** Copa, Champions, Europa, Conference (2022-26)
- **Competiciones Femeninas:** Copa Reina, Supercopa Fem., UWCL (2022-26)

### Actual (2026-27 - Incremental)

- **Primera División:** Actualización diaria de jornadas
- **Segunda División:** Actualización diaria de jornadas
- **Liga F:** Actualización diaria de jornadas
- **Competiciones:** Actualización según se jueguen

### Complementarios

- **Plantillas:** 50+ equipos del universo seguido (2026-27)
- **Estadios:** 150+ estadios con geocodificación (latitud/longitud)
- **Clima:** 
  - Observaciones reales (después de partidos)
  - Previsiones T-24h (24 horas antes)
  - Previsiones T-72h (72 horas antes)
  - Fuente: Open-Meteo API

## Flujo de Datos Típico

### Ejemplo: Ingesta diaria Primera División

```
1. GitHub Action (actualizar_laliga.yml) dispara
2. Python script: laliga_partidos.py
   • Descarga datos de laliga.com
   • Parsea HTML/JSON
   • Valida volumen (380 partidos)
   • Abre lote en IONOS API
3. API IONOS (guardar_partido.php)
   • Valida estructura
   • Crea/actualiza equipos si es necesario
   • Guarda en nucleo_partidos
   • Guarda RAW en bruto_respuestas_api
4. MariaDB actualiza estado
   • PROGRAMADO → FINALIZADO cuando se juega
   • Mantiene idempotencia para reruns
```

## Reglas Anti Data-Leakage

Para predicción point-in-time correcta:

- ✅ Usar `available_at` / `known_at` para verificar qué se sabía antes del partido
- ✅ Previsiones meteorológicas: T-24h y T-72h (no usar observación real como si fuera previsión)
- ✅ Handicap histórico: datos disponibles ANTES del partido, no después
- ✅ Clasificación: snapshot del momento del partido, no clasificación final
- ✅ Lesiones: información publicada antes, no confirmada después

## Endpoints PHP Principales

Alojados en `/api/ingesta/`:

| Endpoint | Método | Propósito |
|----------|--------|-----------|
| `bootstrap.php` | GET | Verificar BD y versión |
| `health.php` | GET | Health check |
| `iniciar_lote.php` | POST | Abrir lote de ingesta |
| `finalizar_lote.php` | POST | Cerrar lote |
| `contexto_partidos.php` | GET | Estado de partidos a cargar |
| `guardar_partido.php` | POST | Guardar partido (liga) |
| `guardar_partido_complementario.php` | POST | Guardar partido (copa/UEFA) |
| `guardar_clima.php` | POST | Guardar clima actual |
| `guardar_clima_historico.php` | POST | Guardar previsión histórica |
| `guardar_estadio.php` | POST | Guardar estadio con coordenadas |
| `guardar_plantilla.php` | POST | Guardar plantilla de equipo |
| `guardar_documento.php` | POST | Guardar documento RAW |

## Tabla de Datos Central

`nucleo_partidos` es la tabla más referenciada por foreign keys (47 tablas referencian su `partido_id`). Campos principales:

```
partido_id              [PK]
temporada_competicion_id [FK → nucleo_temporadas_competicion]
equipo_local_id         [FK → nucleo_equipos]
equipo_visitante_id     [FK → nucleo_equipos]
estadio_id              [FK → nucleo_estadios]
fecha_hora_inicio       [Hora local de Madrid]
zona_horaria            ['Europe/Madrid']
estado                  ['PROGRAMADO', 'FINALIZADO', 'APLAZADO', 'SUSPENDIDO', ...]
goles_local / goles_visitante
hubo_prorroga / hubo_penaltis
arbitro_id
```

## Validación y Métricas

### Conteos Validados (Estado 03/09/2026)

- ✅ Primera 2022-26: 1,900 partidos
- ✅ Segunda 2022-26: 2,310 partidos
- ✅ Liga F 2022-26: 2,400 partidos
- ✅ Copa Masculina 2022-26: 420+ partidos
- ✅ Champions 2022-26: 150+ partidos
- ✅ UWCL 2022-26: 110+ partidos
- ✅ Clima histórico: 235/237 partidos (2 con temperatura NULL documentado)

### Política de Reruns

- ✅ Idempotente: ejecutar múltiples veces produce el mismo resultado
- ✅ PROGRAMADO → FINALIZADO es seguro
- ⚠️ FINALIZADO → PROGRAMADO NUNCA (degradación de datos)
- ✅ Guardado incremental: no sobrescribe datos existentes sin validación

## Troubleshooting

### GitHub Actions falla con "API IONOS HTTP 502"

Reintentará automáticamente (3 intentos con backoff exponencial).

### Clima histórico incompleto

Ejecutar:
```bash
python3 ingestion/clima_historico.py
```

Repite automáticamente hasta que `contexto_clima_historico.php` retorne `pendiente = 0`.

### Duplicados en competiciones complementarias

Ejecutar validación (SQL en documentación):
```sql
SELECT COUNT(*) FROM nucleo_partidos WHERE [...] GROUP BY [...]
```

Compararar con conteos esperados (tabla de validación arriba).

## Próximos Pasos (Roadmap)

### ✅ Completado
- Ingesta histórica de todas las ligas
- Ingesta actual 2026-27 (incremental)
- Clima (actual, histórico con pequeña brecha)
- Plantillas, estadios

### 🚧 En Desarrollo
- Clima histórico: 2 partidos con temperatura NULL (registrado, no error)
- Schema ENGINE/CHARSET: consistencia
- `.gitignore` y documentación

### 📋 Próximo: Features & Modelo
- Ingeniería de features (Elo, forma, descanso, carga)
- Materialización en `analitica_contexto_partido`
- Modelo predictivo 1-X-2
- Backtesting point-in-time

## Contribuir

1. **Crear rama** para cambios
2. **Ejecutar localmente** con `INGEST_API_URL` de testing si es necesario
3. **Validar volumen** antes de hacer POST a API
4. **Mantener idempotencia** en todos los scripts
5. **Documentar** cambios en data leakage o estructura

## Licencia

Privado - Proyecto Juan Carlos Mallo

## Contacto

Para preguntas sobre arquitectura, ingesta o datos: Consultar documentación del proyecto (HANDOFF_*.md)
