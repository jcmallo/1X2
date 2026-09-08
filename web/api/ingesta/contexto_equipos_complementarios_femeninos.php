<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

$pdo = db();

/*
 * Universo objetivo:
 * TODOS los equipos femeninos que hayan participado en Liga F en cualquiera
 * de las cinco temporadas cubiertas por el proyecto.
 *
 * No se limita a la temporada solicitada porque un club ya conocido puede
 * aparecer como rival de Copa aunque esa campaña no esté en Liga F.
 */
$sql = "
    SELECT DISTINCT
        e.equipo_id,
        e.nombre_canonico,
        x.id_equipo_fuente AS soccerdonna_id
    FROM nucleo_equipos e
    INNER JOIN nucleo_equipos_temporada_competicion etc
        ON etc.equipo_id = e.equipo_id
    INNER JOIN nucleo_temporadas_competicion tc
        ON tc.temporada_competicion_id = etc.temporada_competicion_id
    INNER JOIN nucleo_competiciones c
        ON c.competicion_id = tc.competicion_id
    INNER JOIN nucleo_temporadas t
        ON t.temporada_id = tc.temporada_id
    LEFT JOIN nucleo_ids_externos_equipo x
        ON x.equipo_id = e.equipo_id
       AND x.fuente = 'soccerdonna.de'
    WHERE e.genero = 'FEMENINO'
      AND c.nombre = 'Liga F'
      AND c.genero = 'FEMENINO'
      AND t.etiqueta IN ('2022-23','2023-24','2024-25','2025-26','2026-27')
    ORDER BY e.nombre_canonico
";

$stmt = $pdo->query($sql);

$items = [];
foreach ($stmt->fetchAll() as $row) {
    $items[] = [
        'equipo_id' => (int)$row['equipo_id'],
        'nombre_canonico' => (string)$row['nombre_canonico'],
        'soccerdonna_id' => (
            $row['soccerdonna_id'] !== null
            ? (string)$row['soccerdonna_id']
            : null
        ),
    ];
}

api_json([
    'ok' => true,
    'items' => $items,
    'total' => count($items),
]);
