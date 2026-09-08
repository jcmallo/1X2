<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

$limite = isset($_GET['limite']) ? (int)$_GET['limite'] : 500;
$limite = max(1, min(1000, $limite));

$sql = "
    SELECT
        p.partido_id,
        p.fecha_hora_inicio,
        p.jornada_numero,
        p.estado,
        e.estadio_id,
        e.nombre AS estadio,
        e.ciudad,
        e.latitud,
        e.longitud,
        c.nombre AS competicion,
        t.etiqueta AS temporada,
        el.nombre_canonico AS local,
        ev.nombre_canonico AS visitante,

        CASE WHEN EXISTS (
            SELECT 1
            FROM clima_observaciones o
            WHERE o.partido_id = p.partido_id
              AND o.fuente = 'open-meteo-historical'
        ) THEN 0 ELSE 1 END AS falta_observacion,

        CASE WHEN EXISTS (
            SELECT 1
            FROM clima_previsiones cp
            WHERE cp.partido_id = p.partido_id
              AND cp.fuente = 'open-meteo-previous-runs'
              AND cp.horas_antelacion = 24
        ) THEN 0 ELSE 1 END AS falta_prev24,

        CASE WHEN EXISTS (
            SELECT 1
            FROM clima_previsiones cp
            WHERE cp.partido_id = p.partido_id
              AND cp.fuente = 'open-meteo-previous-runs'
              AND cp.horas_antelacion = 72
        ) THEN 0 ELSE 1 END AS falta_prev72

    FROM nucleo_partidos p
    JOIN nucleo_estadios e
        ON e.estadio_id = p.estadio_id
    JOIN nucleo_temporadas_competicion tc
        ON tc.temporada_competicion_id = p.temporada_competicion_id
    JOIN nucleo_competiciones c
        ON c.competicion_id = tc.competicion_id
    JOIN nucleo_temporadas t
        ON t.temporada_id = tc.temporada_id
    JOIN nucleo_equipos el
        ON el.equipo_id = p.equipo_local_id
    JOIN nucleo_equipos ev
        ON ev.equipo_id = p.equipo_visitante_id

    WHERE p.estado = 'FINALIZADO'
      AND p.fecha_hora_inicio IS NOT NULL
      AND e.latitud IS NOT NULL
      AND e.longitud IS NOT NULL
      AND (
          NOT EXISTS (
              SELECT 1
              FROM clima_observaciones o
              WHERE o.partido_id = p.partido_id
                AND o.fuente = 'open-meteo-historical'
          )
          OR NOT EXISTS (
              SELECT 1
              FROM clima_previsiones cp
              WHERE cp.partido_id = p.partido_id
                AND cp.fuente = 'open-meteo-previous-runs'
                AND cp.horas_antelacion = 24
          )
          OR NOT EXISTS (
              SELECT 1
              FROM clima_previsiones cp
              WHERE cp.partido_id = p.partido_id
                AND cp.fuente = 'open-meteo-previous-runs'
                AND cp.horas_antelacion = 72
          )
      )

    ORDER BY p.fecha_hora_inicio ASC, p.partido_id ASC
    LIMIT $limite
";

$items = db()->query($sql)->fetchAll();

foreach ($items as &$item) {
    $item['falta_observacion'] = (int)$item['falta_observacion'];
    $item['falta_prev24'] = (int)$item['falta_prev24'];
    $item['falta_prev72'] = (int)$item['falta_prev72'];
}
unset($item);

api_json([
    'ok' => true,
    'count' => count($items),
    'items' => $items,
]);
