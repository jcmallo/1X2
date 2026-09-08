<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

/**
 * Resultados de partidos finalizados, para entrenar el modelo propio.
 *
 * Devuelve solo partidos con estado FINALIZADO y goles cargados: lo que hace
 * falta para calcular Elo, forma y ventaja de campo sin tocar la base
 * directamente.
 *
 * Parámetros:
 *   competicion_id  int     filtra por competición (opcional)
 *   genero          string  MASCULINO | FEMENINO | MIXTO (opcional)
 *   temporada       string  etiqueta, p.ej. '2025-26' (opcional)
 *   desde           date    fecha mínima del partido (opcional)
 *   solo_quiniela   '1'     limita a competiciones con apta_quiniela = 1
 *   limite          1..5000 (por defecto 2000)
 *   offset          para paginar
 *
 * Orden estable por fecha y partido_id: paginando con offset no se salta ni
 * se repite ningún partido.
 */

$competicionId = null;
if (isset($_GET['competicion_id']) && $_GET['competicion_id'] !== '') {
    if (filter_var($_GET['competicion_id'], FILTER_VALIDATE_INT) === false) {
        api_fail('competicion_id debe ser entero.', 422);
    }
    $competicionId = (int)$_GET['competicion_id'];
}

$genero = '';
if (isset($_GET['genero']) && $_GET['genero'] !== '') {
    $genero = strtoupper(trim((string)$_GET['genero']));
    if (!in_array($genero, ['MASCULINO', 'FEMENINO', 'MIXTO'], true)) {
        api_fail('genero debe ser MASCULINO, FEMENINO o MIXTO.', 422);
    }
}

$temporada = isset($_GET['temporada']) ? trim((string)$_GET['temporada']) : '';

$desde = null;
if (isset($_GET['desde']) && $_GET['desde'] !== '') {
    $desde = normalize_datetime((string)$_GET['desde'], 'desde');
}

$soloQuiniela = isset($_GET['solo_quiniela']) && (string)$_GET['solo_quiniela'] === '1';

$limite = isset($_GET['limite']) ? (int)$_GET['limite'] : 2000;
$limite = max(1, min(5000, $limite));

$offset = isset($_GET['offset']) ? (int)$_GET['offset'] : 0;
$offset = max(0, $offset);

$where = "
    WHERE p.estado = 'FINALIZADO'
      AND p.goles_local IS NOT NULL
      AND p.goles_visitante IS NOT NULL
";
$params = [];

if ($competicionId !== null) {
    $where .= ' AND c.competicion_id = :competicion_id ';
    $params[':competicion_id'] = $competicionId;
}
if ($genero !== '') {
    $where .= ' AND c.genero = :genero ';
    $params[':genero'] = $genero;
}
if ($temporada !== '') {
    $where .= ' AND t.etiqueta = :temporada ';
    $params[':temporada'] = $temporada;
}
if ($desde !== null) {
    $where .= ' AND p.fecha_hora_inicio >= :desde ';
    $params[':desde'] = $desde;
}
if ($soloQuiniela) {
    $where .= ' AND c.apta_quiniela = 1 ';
}

$sql = "
    SELECT
        p.partido_id,
        p.fecha_hora_inicio,
        p.equipo_local_id,
        el.nombre_canonico AS equipo_local,
        p.equipo_visitante_id,
        ev.nombre_canonico AS equipo_visitante,
        p.goles_local,
        p.goles_visitante,
        CASE
            WHEN p.goles_local > p.goles_visitante THEN '1'
            WHEN p.goles_local = p.goles_visitante THEN 'X'
            ELSE '2'
        END AS signo,
        c.competicion_id,
        c.nombre AS competicion,
        c.genero,
        t.etiqueta AS temporada
    FROM nucleo_partidos p
    JOIN nucleo_equipos el ON el.equipo_id = p.equipo_local_id
    JOIN nucleo_equipos ev ON ev.equipo_id = p.equipo_visitante_id
    JOIN nucleo_temporadas_competicion tc
        ON tc.temporada_competicion_id = p.temporada_competicion_id
    JOIN nucleo_competiciones c ON c.competicion_id = tc.competicion_id
    JOIN nucleo_temporadas t ON t.temporada_id = tc.temporada_id
    $where
    ORDER BY p.fecha_hora_inicio ASC, p.partido_id ASC
    LIMIT $limite OFFSET $offset
";

try {
    $stmt = db()->prepare($sql);
    $stmt->execute($params);
    $items = $stmt->fetchAll();

    $sqlTotal = "
        SELECT COUNT(*)
        FROM nucleo_partidos p
        JOIN nucleo_temporadas_competicion tc
            ON tc.temporada_competicion_id = p.temporada_competicion_id
        JOIN nucleo_competiciones c ON c.competicion_id = tc.competicion_id
        JOIN nucleo_temporadas t ON t.temporada_id = tc.temporada_id
        $where
    ";
    $stmt = db()->prepare($sqlTotal);
    $stmt->execute($params);
    $total = (int)$stmt->fetchColumn();
} catch (Throwable $e) {
    error_log('contexto_resultados.php: ' . $e->getMessage());
    api_fail('No se pudieron consultar los resultados.', 500);
}

api_json([
    'ok' => true,
    'total' => $total,
    'offset' => $offset,
    'count' => count($items),
    'hay_mas' => ($offset + count($items)) < $total,
    'items' => $items,
]);
