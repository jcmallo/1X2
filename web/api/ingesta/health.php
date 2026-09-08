<?php
declare(strict_types=1);
require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

try {
    $pdo = db();
    $row = $pdo->query(
        "SELECT DATABASE() AS base_datos, VERSION() AS version_mariadb, UTC_TIMESTAMP() AS utc_db"
    )->fetch();

    api_json([
        'ok' => true,
        'service' => 'quiniela-1x2-ingesta',
        'database' => $row['base_datos'] ?? null,
        'db_version' => $row['version_mariadb'] ?? null,
        'db_utc' => $row['utc_db'] ?? null,
        'php' => PHP_VERSION,
    ]);
} catch (Throwable $e) {
    error_log('health.php: ' . $e->getMessage());
    api_fail('Error comprobando el servicio.', 500);
}
