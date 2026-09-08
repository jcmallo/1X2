<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

$soloPendientes = !isset($_GET['solo_pendientes'])
    || (string)$_GET['solo_pendientes'] !== '0';

$limite = isset($_GET['limite']) ? (int)$_GET['limite'] : 100;
$limite = max(1, min(200, $limite));

$where = "
    WHERE e.nombre IS NOT NULL
      AND TRIM(e.nombre) <> ''
      AND LOWER(TRIM(e.nombre)) <> 'h'
";

if ($soloPendientes) {
    $where .= "
      AND (e.latitud IS NULL OR e.longitud IS NULL)
    ";
}

$sql = "
    SELECT
        e.estadio_id,
        e.nombre,
        e.ciudad,
        e.latitud,
        e.longitud,
        e.coordenadas_verificadas,
        e.fuente_coordenadas,
        e.notas_calidad,
        MAX(p.fecha_hora_inicio) AS ultimo_partido_conocido,
        GROUP_CONCAT(
            DISTINCT CONCAT(
                el.nombre_canonico,
                ' [',
                c.nombre,
                ']'
            )
            ORDER BY el.nombre_canonico
            SEPARATOR ' | '
        ) AS contexto_equipos
    FROM nucleo_estadios e
    LEFT JOIN nucleo_partidos p
        ON p.estadio_id = e.estadio_id
    LEFT JOIN nucleo_equipos el
        ON el.equipo_id = p.equipo_local_id
    LEFT JOIN nucleo_temporadas_competicion tc
        ON tc.temporada_competicion_id = p.temporada_competicion_id
    LEFT JOIN nucleo_competiciones c
        ON c.competicion_id = tc.competicion_id
    $where
    GROUP BY
        e.estadio_id,
        e.nombre,
        e.ciudad,
        e.latitud,
        e.longitud,
        e.coordenadas_verificadas,
        e.fuente_coordenadas,
        e.notas_calidad
    ORDER BY
        CASE
            WHEN e.latitud IS NULL OR e.longitud IS NULL THEN 0
            ELSE 1
        END,
        ultimo_partido_conocido DESC,
        e.estadio_id
    LIMIT $limite
";

$items = db()->query($sql)->fetchAll();

api_json([
    'ok' => true,
    'solo_pendientes' => $soloPendientes,
    'count' => count($items),
    'items' => $items,
]);
