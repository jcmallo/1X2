<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

/**
 * Guarda una jornada de La Quiniela con sus casillas.
 *
 * Payload:
 *   numero_jornada      int      obligatorio
 *   etiqueta_temporada  string   obligatorio (ej. '2026-27')
 *   fecha_sorteo        date     obligatorio (YYYY-MM-DD)
 *   publicado_en        datetime opcional
 *   fuente              string   opcional, por defecto 'selae'
 *   origen_respuesta_id int      opcional
 *   casillas            array    obligatorio, 1..15 elementos:
 *       posicion                  int 1..15  obligatorio
 *       equipo_local_impreso      string     obligatorio
 *       equipo_visitante_impreso  string     obligatorio
 *       partido_id                int        opcional (vínculo a nucleo_partidos)
 *       signo_oficial             '1'|'X'|'2'      opcional
 *       signo_pleno_oficial       '0'|'1'|'2'|'M'  opcional (solo posición 15)
 *
 * Reglas:
 *   - Idempotente: UNIQUE(numero_jornada, etiqueta_temporada) y
 *     UNIQUE(jornada_id, posicion).
 *   - Transaccional: o entra todo o no entra nada.
 *   - Anti-degradación: un signo_oficial ya guardado NO se borra con un NULL.
 *     Solo se sobrescribe si llega un valor nuevo.
 */

$numeroJornada = required_int($data, 'numero_jornada');
if ($numeroJornada < 1 || $numeroJornada > 60) {
    api_fail('numero_jornada fuera de rango (1..60).', 422);
}

$etiquetaTemporada = str_required($data, 'etiqueta_temporada', 20);

$fechaSorteo = normalize_datetime(
    isset($data['fecha_sorteo']) ? (string)$data['fecha_sorteo'] : null,
    'fecha_sorteo'
);
if ($fechaSorteo === null) {
    api_fail('fecha_sorteo es obligatoria.', 422);
}
$fechaSorteo = substr($fechaSorteo, 0, 10);

$publicadoEn = normalize_datetime(
    isset($data['publicado_en']) ? (string)$data['publicado_en'] : null,
    'publicado_en'
);

$fuente = isset($data['fuente']) && $data['fuente'] !== null && $data['fuente'] !== ''
    ? str_required($data, 'fuente', 50)
    : 'selae';

$origenRespuestaId = nullable_int($data, 'origen_respuesta_id');

if (!isset($data['casillas']) || !is_array($data['casillas']) || $data['casillas'] === []) {
    api_fail('casillas debe ser un array con al menos un elemento.', 422);
}
if (count($data['casillas']) > 15) {
    api_fail('casillas no puede tener más de 15 elementos.', 422);
}

// --- Validación de casillas ---------------------------------------------

$signosValidos = ['1', 'X', '2'];
$plenosValidos = ['0', '1', '2', 'M'];
$casillas = [];
$posicionesVistas = [];

foreach ($data['casillas'] as $i => $c) {
    if (!is_array($c)) {
        api_fail("casillas[$i] debe ser un objeto.", 422);
    }

    $posicion = required_int($c, 'posicion');
    if ($posicion < 1 || $posicion > 15) {
        api_fail("casillas[$i].posicion fuera de rango (1..15).", 422);
    }
    if (isset($posicionesVistas[$posicion])) {
        api_fail("posicion $posicion duplicada en el payload.", 422);
    }
    $posicionesVistas[$posicion] = true;

    $local     = str_required($c, 'equipo_local_impreso', 100);
    $visitante = str_required($c, 'equipo_visitante_impreso', 100);

    $partidoId = nullable_int($c, 'partido_id');

    $signo = null;
    if (isset($c['signo_oficial']) && $c['signo_oficial'] !== null && $c['signo_oficial'] !== '') {
        $signo = strtoupper(trim((string)$c['signo_oficial']));
        if (!in_array($signo, $signosValidos, true)) {
            api_fail("casillas[$i].signo_oficial debe ser 1, X o 2.", 422);
        }
    }

    $signoPleno = null;
    if (isset($c['signo_pleno_oficial']) && $c['signo_pleno_oficial'] !== null && $c['signo_pleno_oficial'] !== '') {
        $signoPleno = strtoupper(trim((string)$c['signo_pleno_oficial']));
        if (!in_array($signoPleno, $plenosValidos, true)) {
            api_fail("casillas[$i].signo_pleno_oficial debe ser 0, 1, 2 o M.", 422);
        }
        if ($posicion !== 15) {
            api_fail('signo_pleno_oficial solo es válido en la posición 15.', 422);
        }
    }

    $casillas[] = [
        'posicion' => $posicion,
        'partido_id' => $partidoId,
        'local' => $local,
        'visitante' => $visitante,
        'signo' => $signo,
        'signo_pleno' => $signoPleno,
    ];
}

// --- Validación de partido_id contra nucleo_partidos ---------------------

$idsPartido = array_values(array_filter(
    array_map(static fn(array $c) => $c['partido_id'], $casillas),
    static fn($v) => $v !== null
));

if ($idsPartido !== []) {
    $ph = implode(',', array_fill(0, count($idsPartido), '?'));
    $stmt = db()->prepare(
        "SELECT partido_id FROM nucleo_partidos WHERE partido_id IN ($ph)"
    );
    $stmt->execute($idsPartido);
    $encontrados = array_map('intval', $stmt->fetchAll(PDO::FETCH_COLUMN));

    $faltan = array_diff($idsPartido, $encontrados);
    if ($faltan !== []) {
        api_fail(
            'partido_id inexistentes en nucleo_partidos: '
            . implode(', ', $faltan),
            422
        );
    }
}

// --- Guardado transaccional ----------------------------------------------

try {
    $pdo = db();
    $pdo->beginTransaction();

    $stmt = $pdo->prepare("
        SELECT jornada_id
        FROM quiniela_jornadas
        WHERE numero_jornada = :numero AND etiqueta_temporada = :temporada
        LIMIT 1
    ");
    $stmt->execute([
        ':numero' => $numeroJornada,
        ':temporada' => $etiquetaTemporada,
    ]);
    $jornadaId = $stmt->fetchColumn();

    if ($jornadaId === false) {
        $stmt = $pdo->prepare("
            INSERT INTO quiniela_jornadas
                (numero_jornada, etiqueta_temporada, fecha_sorteo,
                 publicado_en, fuente, origen_respuesta_id)
            VALUES
                (:numero, :temporada, :fecha_sorteo,
                 :publicado_en, :fuente, :origen)
        ");
        $stmt->execute([
            ':numero' => $numeroJornada,
            ':temporada' => $etiquetaTemporada,
            ':fecha_sorteo' => $fechaSorteo,
            ':publicado_en' => $publicadoEn,
            ':fuente' => $fuente,
            ':origen' => $origenRespuestaId,
        ]);
        $jornadaId = (int)$pdo->lastInsertId();
        $accionJornada = 'creada';
    } else {
        $jornadaId = (int)$jornadaId;
        $stmt = $pdo->prepare("
            UPDATE quiniela_jornadas SET
                fecha_sorteo = :fecha_sorteo,
                publicado_en = COALESCE(:publicado_en, publicado_en),
                fuente = :fuente,
                origen_respuesta_id = COALESCE(:origen, origen_respuesta_id)
            WHERE jornada_id = :id
        ");
        $stmt->execute([
            ':fecha_sorteo' => $fechaSorteo,
            ':publicado_en' => $publicadoEn,
            ':fuente' => $fuente,
            ':origen' => $origenRespuestaId,
            ':id' => $jornadaId,
        ]);
        $accionJornada = 'actualizada';
    }

    $insert = $pdo->prepare("
        INSERT INTO quiniela_partidos_jornada
            (jornada_id, posicion, partido_id,
             equipo_local_impreso, equipo_visitante_impreso,
             signo_oficial, signo_pleno_oficial)
        VALUES
            (:jornada_id, :posicion, :partido_id,
             :local, :visitante, :signo, :signo_pleno)
        ON DUPLICATE KEY UPDATE
            partido_id = COALESCE(VALUES(partido_id), partido_id),
            equipo_local_impreso = VALUES(equipo_local_impreso),
            equipo_visitante_impreso = VALUES(equipo_visitante_impreso),
            signo_oficial = COALESCE(VALUES(signo_oficial), signo_oficial),
            signo_pleno_oficial = COALESCE(VALUES(signo_pleno_oficial), signo_pleno_oficial)
    ");

    foreach ($casillas as $c) {
        $insert->execute([
            ':jornada_id' => $jornadaId,
            ':posicion' => $c['posicion'],
            ':partido_id' => $c['partido_id'],
            ':local' => $c['local'],
            ':visitante' => $c['visitante'],
            ':signo' => $c['signo'],
            ':signo_pleno' => $c['signo_pleno'],
        ]);
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'accion' => $accionJornada,
        'jornada_id' => $jornadaId,
        'casillas_procesadas' => count($casillas),
    ], $accionJornada === 'creada' ? 201 : 200);
} catch (Throwable $e) {
    if (isset($pdo) && $pdo->inTransaction()) {
        $pdo->rollBack();
    }
    error_log('guardar_jornada_quiniela.php: ' . $e->getMessage());
    api_fail('No se pudo guardar la jornada de quiniela.', 500);
}
