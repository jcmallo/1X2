<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

$pdo = db();

$competicion = isset($_GET['competicion']) ? trim((string)$_GET['competicion']) : 'Liga F';
$temporada = isset($_GET['temporada']) ? trim((string)$_GET['temporada']) : '2026-27';
$genero = isset($_GET['genero']) ? strtoupper(trim((string)$_GET['genero'])) : 'FEMENINO';

if ($competicion === '' || strlen($competicion) > 150) {
    api_fail('competicion inválida.', 422);
}
if ($temporada === '' || strlen($temporada) > 20) {
    api_fail('temporada inválida.', 422);
}
if (!in_array($genero, ['MASCULINO', 'FEMENINO', 'MIXTO'], true)) {
    api_fail('genero inválido.', 422);
}

$retroHoras = isset($_GET['retro_horas']) ? (int)$_GET['retro_horas'] : 72;
$retroHoras = max(12, min(336, $retroHoras));

$futuroDias = isset($_GET['futuro_dias']) ? (int)$_GET['futuro_dias'] : 21;
$futuroDias = max(3, min(60, $futuroDias));

$adelanteJornadas = isset($_GET['adelante_jornadas']) ? (int)$_GET['adelante_jornadas'] : 3;
$adelanteJornadas = max(1, min(6, $adelanteJornadas));

$reconciliar = isset($_GET['reconciliar'])
    && in_array(strtolower((string)$_GET['reconciliar']), ['1', 'true', 'si', 'sí', 'yes'], true);

$ultimasJornadas = isset($_GET['ultimas_jornadas']) ? (int)$_GET['ultimas_jornadas'] : 4;
$ultimasJornadas = max(1, min(10, $ultimasJornadas));

$stmt = $pdo->prepare("
    SELECT
        tc.temporada_competicion_id,
        tc.numero_jornadas
    FROM nucleo_temporadas_competicion tc
    INNER JOIN nucleo_competiciones c
        ON c.competicion_id = tc.competicion_id
    INNER JOIN nucleo_temporadas t
        ON t.temporada_id = tc.temporada_id
    WHERE c.nombre = :competicion
      AND c.genero = :genero
      AND t.etiqueta = :temporada
    LIMIT 1
");
$stmt->execute([
    ':competicion' => $competicion,
    ':genero' => $genero,
    ':temporada' => $temporada,
]);
$ctx = $stmt->fetch();

if (!$ctx) {
    // Bootstrap de una competición/temporada todavía vacía.
    $iniciales = [];
    for ($j = 1; $j <= $adelanteJornadas; $j++) {
        $iniciales[] = $j;
    }

    api_json([
        'ok' => true,
        'competicion' => $competicion,
        'temporada' => $temporada,
        'genero' => $genero,
        'temporada_competicion_id' => null,
        'max_jornada_cargada' => 0,
        'numero_jornadas' => null,
        'jornadas' => $iniciales,
        'motivo' => 'bootstrap',
    ]);
}

$tcId = (int)$ctx['temporada_competicion_id'];
$numeroJornadas = $ctx['numero_jornadas'] !== null ? (int)$ctx['numero_jornadas'] : 30;

$stmt = $pdo->prepare("
    SELECT MAX(jornada_numero) AS max_jornada
    FROM nucleo_partidos
    WHERE temporada_competicion_id = :tc
      AND jornada_numero IS NOT NULL
");
$stmt->execute([':tc' => $tcId]);
$maxJornada = (int)($stmt->fetchColumn() ?: 0);

$stmt = $pdo->prepare("
    SELECT DISTINCT jornada_numero
    FROM nucleo_partidos
    WHERE temporada_competicion_id = :tc
      AND jornada_numero IS NOT NULL
      AND (
            estado IN ('EN_JUEGO', 'APLAZADO', 'SUSPENDIDO')
         OR (
                estado = 'PROGRAMADO'
            AND fecha_hora_inicio >= DATE_SUB(NOW(), INTERVAL :retro_horas HOUR)
            AND fecha_hora_inicio <= DATE_ADD(NOW(), INTERVAL :futuro_dias DAY)
         )
         OR (
                estado IN ('FINALIZADO', 'CANCELADO', 'ADJUDICADO')
            AND fecha_hora_inicio >= DATE_SUB(NOW(), INTERVAL :retro_horas2 HOUR)
         )
      )
    ORDER BY jornada_numero
");
$stmt->bindValue(':tc', $tcId, PDO::PARAM_INT);
$stmt->bindValue(':retro_horas', $retroHoras, PDO::PARAM_INT);
$stmt->bindValue(':futuro_dias', $futuroDias, PDO::PARAM_INT);
$stmt->bindValue(':retro_horas2', $retroHoras, PDO::PARAM_INT);
$stmt->execute();

$jornadas = [];
foreach ($stmt->fetchAll() as $row) {
    $j = (int)$row['jornada_numero'];
    if ($j >= 1 && $j <= $numeroJornadas) {
        $jornadas[$j] = true;
    }
}

// Descubrimiento: revisar algunas jornadas inmediatamente posteriores
// a la última que ya existe en la base, para captar horarios nuevos.
$inicioDescubrimiento = max(1, $maxJornada + 1);
$finDescubrimiento = min($numeroJornadas, $maxJornada + $adelanteJornadas);
for ($j = $inicioDescubrimiento; $j <= $finDescubrimiento; $j++) {
    $jornadas[$j] = true;
}

// Si todavía no hay partidos, arrancar desde las primeras jornadas.
if ($maxJornada === 0) {
    for ($j = 1; $j <= min($numeroJornadas, $adelanteJornadas); $j++) {
        $jornadas[$j] = true;
    }
}

// Reconciliación semanal: vuelve a comprobar unas pocas jornadas recientes,
// no toda la temporada.
if ($reconciliar && $maxJornada > 0) {
    $desde = max(1, $maxJornada - $ultimasJornadas + 1);
    for ($j = $desde; $j <= $maxJornada; $j++) {
        $jornadas[$j] = true;
    }
}

$seleccion = array_keys($jornadas);
sort($seleccion, SORT_NUMERIC);

api_json([
    'ok' => true,
    'competicion' => $competicion,
    'temporada' => $temporada,
    'genero' => $genero,
    'temporada_competicion_id' => $tcId,
    'max_jornada_cargada' => $maxJornada,
    'numero_jornadas' => $numeroJornadas,
    'retro_horas' => $retroHoras,
    'futuro_dias' => $futuroDias,
    'adelante_jornadas' => $adelanteJornadas,
    'reconciliar' => $reconciliar,
    'jornadas' => $seleccion,
]);
