<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

/**
 * Devuelve jornadas de La Quiniela con sus 15 casillas.
 *
 * Parámetros:
 *   temporada        etiqueta de temporada (opcional; si falta, las más recientes)
 *   numero_jornada   número concreto (opcional)
 *   solo_pendientes  '1' devuelve solo jornadas sin signos oficiales
 *   limite           1..50 (por defecto 5)
 */

$temporada = isset($_GET['temporada']) ? trim((string)$_GET['temporada']) : '';

$numeroJornada = null;
if (isset($_GET['numero_jornada']) && $_GET['numero_jornada'] !== '') {
    if (filter_var($_GET['numero_jornada'], FILTER_VALIDATE_INT) === false) {
        api_fail('numero_jornada debe ser entero.', 422);
    }
    $numeroJornada = (int)$_GET['numero_jornada'];
}

$soloPendientes = isset($_GET['solo_pendientes'])
    && (string)$_GET['solo_pendientes'] === '1';

$limite = isset($_GET['limite']) ? (int)$_GET['limite'] : 5;
$limite = max(1, min(50, $limite));

$where = ' WHERE 1=1 ';
$params = [];

if ($temporada !== '') {
    $where .= ' AND j.etiqueta_temporada = :temporada ';
    $params[':temporada'] = $temporada;
}

if ($numeroJornada !== null) {
    $where .= ' AND j.numero_jornada = :numero_jornada ';
    $params[':numero_jornada'] = $numeroJornada;
}

if ($soloPendientes) {
    $where .= "
      AND EXISTS (
          SELECT 1
          FROM quiniela_partidos_jornada pj
          WHERE pj.jornada_id = j.jornada_id
            AND pj.signo_oficial IS NULL
      )
    ";
}

$sqlJornadas = "
    SELECT
        j.jornada_id,
        j.numero_jornada,
        j.etiqueta_temporada,
        j.fecha_sorteo,
        j.publicado_en,
        j.fuente,
        (
            SELECT COUNT(*)
            FROM quiniela_partidos_jornada pj2
            WHERE pj2.jornada_id = j.jornada_id
        ) AS casillas_cargadas,
        (
            SELECT COUNT(*)
            FROM quiniela_partidos_jornada pj3
            WHERE pj3.jornada_id = j.jornada_id
              AND pj3.signo_oficial IS NOT NULL
        ) AS casillas_con_resultado,
        (
            SELECT COUNT(*)
            FROM quiniela_partidos_jornada pj4
            WHERE pj4.jornada_id = j.jornada_id
              AND pj4.partido_id IS NOT NULL
        ) AS casillas_vinculadas
    FROM quiniela_jornadas j
    $where
    ORDER BY j.fecha_sorteo DESC, j.numero_jornada DESC
    LIMIT $limite
";

try {
    $stmt = db()->prepare($sqlJornadas);
    $stmt->execute($params);
    $jornadas = $stmt->fetchAll();

    if ($jornadas === []) {
        api_json([
            'ok' => true,
            'count' => 0,
            'jornadas' => [],
        ]);
    }

    $ids = array_map(static fn(array $j): int => (int)$j['jornada_id'], $jornadas);
    $placeholders = implode(',', array_fill(0, count($ids), '?'));

    $sqlCasillas = "
        SELECT
            pj.jornada_id,
            pj.posicion,
            pj.partido_id,
            pj.equipo_local_impreso,
            pj.equipo_visitante_impreso,
            pj.signo_oficial,
            pj.signo_pleno_oficial,
            p.fecha_hora_inicio,
            p.estado AS estado_partido,
            p.goles_local,
            p.goles_visitante
        FROM quiniela_partidos_jornada pj
        LEFT JOIN nucleo_partidos p
            ON p.partido_id = pj.partido_id
        WHERE pj.jornada_id IN ($placeholders)
        ORDER BY pj.jornada_id, pj.posicion
    ";

    $stmt = db()->prepare($sqlCasillas);
    $stmt->execute($ids);
    $casillas = $stmt->fetchAll();

    $porJornada = [];
    foreach ($casillas as $c) {
        $porJornada[(int)$c['jornada_id']][] = $c;
    }

    foreach ($jornadas as &$j) {
        $j['casillas'] = $porJornada[(int)$j['jornada_id']] ?? [];
    }
    unset($j);
} catch (Throwable $e) {
    error_log('contexto_quiniela.php: ' . $e->getMessage());
    api_fail('No se pudo consultar el contexto de quiniela.', 500);
}

api_json([
    'ok' => true,
    'count' => count($jornadas),
    'jornadas' => $jornadas,
]);
