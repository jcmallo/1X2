<?php
declare(strict_types=1);
require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

$pdo = db();
$modo = isset($_GET['modo']) ? (string)$_GET['modo'] : 'proximos';

if ($modo === 'estadios') {
    // Modo de prueba: devuelve todos los estadios que tengan coordenadas.
    $sql = "
        SELECT
            NULL AS partido_id,
            e.estadio_id,
            e.nombre AS estadio,
            e.ciudad,
            e.latitud,
            e.longitud,
            NULL AS fecha_hora_inicio
        FROM nucleo_estadios e
        WHERE e.latitud IS NOT NULL
          AND e.longitud IS NOT NULL
        ORDER BY e.estadio_id
    ";
    $items = $pdo->query($sql)->fetchAll();

    api_json([
        'ok' => true,
        'modo' => 'estadios',
        'count' => count($items),
        'items' => $items,
    ]);
}

if ($modo !== 'proximos') {
    api_fail('modo debe ser "proximos" o "estadios".', 422);
}

$dias = isset($_GET['dias']) ? (int)$_GET['dias'] : 7;
$dias = max(1, min(14, $dias));

// Partidos futuros con estadio y coordenadas disponibles.
// APLAZADO se excluye deliberadamente hasta disponer de una nueva fecha fiable.
$sql = "
    SELECT
        p.partido_id,
        e.estadio_id,
        e.nombre AS estadio,
        e.ciudad,
        e.latitud,
        e.longitud,
        p.fecha_hora_inicio
    FROM nucleo_partidos p
    INNER JOIN nucleo_estadios e ON e.estadio_id = p.estadio_id
    WHERE p.estado = 'PROGRAMADO'
      AND p.fecha_hora_inicio >= NOW()
      AND p.fecha_hora_inicio < DATE_ADD(NOW(), INTERVAL :dias DAY)
      AND e.latitud IS NOT NULL
      AND e.longitud IS NOT NULL
    ORDER BY p.fecha_hora_inicio, p.partido_id
";

$stmt = $pdo->prepare($sql);
$stmt->bindValue(':dias', $dias, PDO::PARAM_INT);
$stmt->execute();
$items = $stmt->fetchAll();

api_json([
    'ok' => true,
    'modo' => 'proximos',
    'dias' => $dias,
    'count' => count($items),
    'items' => $items,
]);
