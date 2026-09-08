<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

/**
 * Guarda la propuesta de boleto calculada por el optimizador.
 *
 * Payload:
 *   numero_jornada      int      obligatorio
 *   etiqueta_temporada  string   obligatorio
 *   objetivo            string   qué se optimizó, p.ej. 'valor_esperado'
 *   presupuesto_eur     float    opcional
 *   selecciones         array    una entrada por casilla jugada:
 *       posicion    int 1..15
 *       signos      array de '1'|'X'|'2' para las 14 primeras,
 *                   o de '0'|'1'|'2'|'M' para el Pleno al 15
 *
 * La recomendación (FIJO / DOBLE / TRIPLE) se deduce de cuántos signos se
 * marcan, así que no hay que enviarla y no puede quedar incoherente.
 *
 * Cada ejecución se guarda como una fila nueva en optimizador_ejecuciones:
 * las propuestas anteriores no se borran. Interesa conservarlas para poder
 * comparar después lo que se recomendó con lo que ocurrió.
 */

$numeroJornada = required_int($data, 'numero_jornada');
$etiquetaTemporada = str_required($data, 'etiqueta_temporada', 20);

$objetivo = isset($data['objetivo']) && $data['objetivo'] !== ''
    ? str_required($data, 'objetivo', 50)
    : 'valor_esperado';

$presupuesto = nullable_float($data, 'presupuesto_eur');

if (!isset($data['selecciones']) || !is_array($data['selecciones']) || $data['selecciones'] === []) {
    api_fail('selecciones debe ser un array con al menos un elemento.', 422);
}
if (count($data['selecciones']) > 15) {
    api_fail('selecciones no puede tener más de 15 elementos.', 422);
}

$SIGNOS_1X2 = ['1', 'X', '2'];
$SIGNOS_PLENO = ['0', '1', '2', 'M'];

$selecciones = [];
$vistas = [];

foreach ($data['selecciones'] as $i => $s) {
    if (!is_array($s)) {
        api_fail("selecciones[$i] debe ser un objeto.", 422);
    }

    $posicion = required_int($s, 'posicion');
    if ($posicion < 1 || $posicion > 15) {
        api_fail("selecciones[$i].posicion fuera de rango (1..15).", 422);
    }
    if (isset($vistas[$posicion])) {
        api_fail("posicion $posicion duplicada.", 422);
    }
    $vistas[$posicion] = true;

    if (!isset($s['signos']) || !is_array($s['signos']) || $s['signos'] === []) {
        api_fail("selecciones[$i].signos debe ser un array no vacío.", 422);
    }

    $validos = $posicion === 15 ? $SIGNOS_PLENO : $SIGNOS_1X2;
    $signos = [];
    foreach ($s['signos'] as $signo) {
        $v = strtoupper(trim((string)$signo));
        if (!in_array($v, $validos, true)) {
            api_fail(
                "selecciones[$i].signos: '$v' no es válido en la posición "
                . "$posicion (permitidos: " . implode(', ', $validos) . ").",
                422
            );
        }
        if (!in_array($v, $signos, true)) {
            $signos[] = $v;
        }
    }

    if (count($signos) > count($validos)) {
        api_fail("selecciones[$i] tiene más signos de los posibles.", 422);
    }

    $recomendacion = ['FIJO', 'DOBLE', 'TRIPLE'][min(count($signos), 3) - 1];

    $selecciones[] = [
        'posicion' => $posicion,
        'signos' => $signos,
        'recomendacion' => $recomendacion,
    ];
}

try {
    $pdo = db();

    $stmt = $pdo->prepare("
        SELECT jornada_id FROM quiniela_jornadas
        WHERE numero_jornada = :n AND etiqueta_temporada = :t
        LIMIT 1
    ");
    $stmt->execute([':n' => $numeroJornada, ':t' => $etiquetaTemporada]);
    $jornadaId = $stmt->fetchColumn();

    if ($jornadaId === false) {
        api_fail(
            "No existe la jornada $numeroJornada de $etiquetaTemporada. "
            . "Hay que cargarla antes de guardar una propuesta.",
            422
        );
    }
    $jornadaId = (int)$jornadaId;

    // Las casillas deben existir: la propuesta apunta a ellas por id.
    $stmt = $pdo->prepare("
        SELECT posicion, id FROM quiniela_partidos_jornada
        WHERE jornada_id = :j
    ");
    $stmt->execute([':j' => $jornadaId]);
    $idsCasilla = [];
    foreach ($stmt->fetchAll() as $fila) {
        $idsCasilla[(int)$fila['posicion']] = (int)$fila['id'];
    }

    $faltan = [];
    foreach ($selecciones as $s) {
        if (!isset($idsCasilla[$s['posicion']])) {
            $faltan[] = $s['posicion'];
        }
    }
    if ($faltan !== []) {
        api_fail(
            'La jornada no tiene cargadas las casillas: ' . implode(', ', $faltan),
            422
        );
    }

    $pdo->beginTransaction();

    $stmt = $pdo->prepare("
        INSERT INTO optimizador_ejecuciones
            (jornada_id, objetivo, presupuesto_eur)
        VALUES (:j, :o, :p)
    ");
    $stmt->execute([
        ':j' => $jornadaId,
        ':o' => $objetivo,
        ':p' => $presupuesto,
    ]);
    $ejecucionId = (int)$pdo->lastInsertId();

    $ins = $pdo->prepare("
        INSERT INTO optimizador_selecciones_boleto
            (ejecucion_id, partido_jornada_id, recomendacion, signos_incluidos)
        VALUES (:e, :pj, :r, :s)
    ");

    $combinaciones = 1;
    foreach ($selecciones as $s) {
        $ins->execute([
            ':e' => $ejecucionId,
            ':pj' => $idsCasilla[$s['posicion']],
            ':r' => $s['recomendacion'],
            ':s' => json_encode($s['signos'], JSON_UNESCAPED_UNICODE),
        ]);
        $combinaciones *= count($s['signos']);
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'ejecucion_id' => $ejecucionId,
        'jornada_id' => $jornadaId,
        'selecciones' => count($selecciones),
        'combinaciones' => $combinaciones,
    ], 201);
} catch (Throwable $e) {
    if (isset($pdo) && $pdo->inTransaction()) {
        $pdo->rollBack();
    }
    error_log('guardar_boleto.php: ' . $e->getMessage());
    api_fail('No se pudo guardar la propuesta.', 500);
}
