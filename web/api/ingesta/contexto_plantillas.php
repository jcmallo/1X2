<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

$temporada = isset($_GET['temporada']) && is_string($_GET['temporada'])
    ? trim($_GET['temporada'])
    : '2026-27';

$sql = "
    SELECT
        etc.temporada_competicion_id,
        c.competicion_id,
        c.nombre AS competicion,
        c.genero,
        t.temporada_id,
        t.etiqueta AS temporada,
        e.equipo_id,
        e.nombre_canonico,
        e.nombre_corto,
        MAX(CASE
            WHEN ie.fuente = 'laliga.com'
            THEN ie.id_equipo_fuente
        END) AS id_laliga_com,
        MAX(CASE
            WHEN ie.fuente = 'laliga'
            THEN ie.id_equipo_fuente
        END) AS id_laliga_legacy,
        MAX(CASE
            WHEN ie.fuente = 'ligaf.es'
            THEN ie.id_equipo_fuente
        END) AS id_ligaf,
        (
            SELECT COUNT(*)
            FROM nucleo_pertenencias_jugador_equipo p
            WHERE p.equipo_id = e.equipo_id
              AND p.vigente_hasta IS NULL
        ) AS jugadores_activos_actuales
    FROM nucleo_equipos_temporada_competicion etc
    JOIN nucleo_temporadas_competicion tc
        ON tc.temporada_competicion_id = etc.temporada_competicion_id
    JOIN nucleo_competiciones c
        ON c.competicion_id = tc.competicion_id
    JOIN nucleo_temporadas t
        ON t.temporada_id = tc.temporada_id
    JOIN nucleo_equipos e
        ON e.equipo_id = etc.equipo_id
    LEFT JOIN nucleo_ids_externos_equipo ie
        ON ie.equipo_id = e.equipo_id
    WHERE t.etiqueta = :temporada
      AND c.nombre IN ('LaLiga', 'Segunda División', 'Liga F')
    GROUP BY
        etc.temporada_competicion_id,
        c.competicion_id,
        c.nombre,
        c.genero,
        t.temporada_id,
        t.etiqueta,
        e.equipo_id,
        e.nombre_canonico,
        e.nombre_corto
    ORDER BY
        FIELD(c.nombre, 'LaLiga', 'Segunda División', 'Liga F'),
        e.nombre_canonico
";

$stmt = db()->prepare($sql);
$stmt->execute([':temporada' => $temporada]);
$items = $stmt->fetchAll();

api_json([
    'ok' => true,
    'temporada' => $temporada,
    'count' => count($items),
    'items' => $items,
]);
