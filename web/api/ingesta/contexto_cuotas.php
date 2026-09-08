<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

/**
 * Devuelve partidos candidatos a captura de cuotas.
 *
 * Parámetros:
 *   competicion_id    int    filtra por competición (opcional)
 *   temporada         string etiqueta de temporada (opcional)
 *   genero            'MASCULINO'|'FEMENINO'|'MIXTO' (opcional)
 *   solo_quiniela     '1' limita a competiciones con apta_quiniela = 1
 *   dias_futuro       1..30 (por defecto 10)
 *   retro_horas       0..168 (por defecto 0)
 *   solo_sin_cuotas   '1' (por defecto) solo partidos sin ninguna captura
 *   limite            1..500 (por defecto 200)
 */

$competicionId = null;
if (isset($_GET['competicion_id']) && $_GET['competicion_id'] !== '') {
    if (filter_var($_GET['competicion_id'], FILTER_VALIDATE_INT) === false) {
        api_fail('competicion_id debe ser entero.', 422);
    }
    $competicionId = (int)$_GET['competicion_id'];
}

$temporada = isset($_GET['temporada']) ? trim((string)$_GET['temporada']) : '';

$genero = '';
if (isset($_GET['genero']) && $_GET['genero'] !== '') {
    $genero = strtoupper(trim((string)$_GET['genero']));
    if (!in_array($genero, ['MASCULINO', 'FEMENINO', 'MIXTO'], true)) {
        api_fail('genero debe ser MASCULINO, FEMENINO o MIXTO.', 422);
    }
}

$soloQuiniela = isset($_GET['solo_quiniela'])
    && (string)$_GET['solo_quiniela'] === '1';

$diasFuturo = isset($_GET['dias_futuro']) ? (int)$_GET['dias_futuro'] : 10;
$diasFuturo = max(1, min(30, $diasFuturo));

$retroHoras = isset($_GET['retro_horas']) ? (int)$_GET['retro_horas'] : 0;
$retroHoras = max(0, min(168, $retroHoras));

$soloSinCuotas = !isset($_GET['solo_sin_cuotas'])
    || (string)$_GET['solo_sin_cuotas'] !== '0';

$limite = isset($_GET['limite']) ? (int)$_GET['limite'] : 200;
$limite = max(1, min(500, $limite));

$where = "
    WHERE p.estado IN ('PROGRAMADO', 'FINALIZADO')
      AND p.fecha_hora_inicio >= DATE_SUB(NOW(), INTERVAL :retro_horas HOUR)
      AND p.fecha_hora_inicio <= DATE_ADD(NOW(), INTERVAL :dias_futuro DAY)
";

$params = [
    ':retro_horas' => $retroHoras,
    ':dias_futuro' => $diasFuturo,
];

if ($competicionId !== null) {
    $where .= ' AND c.competicion_id = :competicion_id ';
    $params[':competicion_id'] = $competicionId;
}

if ($temporada !== '') {
    $where .= ' AND t.etiqueta = :temporada ';
    $params[':temporada'] = $temporada;
}

if ($genero !== '') {
    $where .= ' AND c.genero = :genero ';
    $params[':genero'] = $genero;
}

if ($soloQuiniela) {
    $where .= ' AND c.apta_quiniela = 1 ';
}

if ($soloSinCuotas) {
    $where .= "
      AND NOT EXISTS (
          SELECT 1
          FROM mercado_cuotas_capturadas mc
          WHERE mc.partido_id = p.partido_id
      )
    ";
}

$sql = "
    SELECT
        p.partido_id,
        p.fecha_hora_inicio,
        p.estado,
        el.equipo_id       AS equipo_local_id,
        el.nombre_canonico AS equipo_local,
        ev.equipo_id       AS equipo_visitante_id,
        ev.nombre_canonico AS equipo_visitante,
        c.competicion_id,
        c.nombre           AS competicion,
        c.genero,
        c.apta_quiniela,
        t.etiqueta         AS temporada,
        TIMESTAMPDIFF(HOUR, NOW(), p.fecha_hora_inicio) AS horas_hasta_inicio,
        (
            SELECT COUNT(*)
            FROM mercado_cuotas_capturadas mc2
            WHERE mc2.partido_id = p.partido_id
        ) AS capturas_existentes,
        (
            SELECT MAX(mc3.capturado_en)
            FROM mercado_cuotas_capturadas mc3
            WHERE mc3.partido_id = p.partido_id
        ) AS ultima_captura
    FROM nucleo_partidos p
    JOIN nucleo_equipos el
        ON el.equipo_id = p.equipo_local_id
    JOIN nucleo_equipos ev
        ON ev.equipo_id = p.equipo_visitante_id
    JOIN nucleo_temporadas_competicion tc
        ON tc.temporada_competicion_id = p.temporada_competicion_id
    JOIN nucleo_competiciones c
        ON c.competicion_id = tc.competicion_id
    JOIN nucleo_temporadas t
        ON t.temporada_id = tc.temporada_id
    $where
    ORDER BY p.fecha_hora_inicio ASC, p.partido_id ASC
    LIMIT $limite
";

try {
    $stmt = db()->prepare($sql);
    $stmt->execute($params);
    $items = $stmt->fetchAll();
} catch (Throwable $e) {
    error_log('contexto_cuotas.php: ' . $e->getMessage());
    api_fail('No se pudo consultar el contexto de cuotas.', 500);
}

api_json([
    'ok' => true,
    'filtros' => [
        'solo_sin_cuotas' => $soloSinCuotas,
        'solo_quiniela' => $soloQuiniela,
        'retro_horas' => $retroHoras,
        'dias_futuro' => $diasFuturo,
    ],
    'count' => count($items),
    'items' => $items,
]);
