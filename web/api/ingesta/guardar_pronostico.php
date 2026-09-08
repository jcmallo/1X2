<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

/**
 * Guarda el pronóstico propio de una jornada.
 *
 * Por qué hace falta un endpoint aparte
 * -------------------------------------
 *
 * importar_jornada_historica.php escribe en la misma tabla, pero fija la
 * fuente a 'BETFAIR_QUINIELANDIA' o 'LAE_*' según de dónde venga el dato.
 * No hay manera de decirle "esto lo ha calculado nuestro modelo", y mezclar
 * el pronóstico propio bajo la etiqueta del mercado haría imposible
 * comparar después uno con otro, que es justo lo que interesa medir.
 *
 * Payload:
 *   numero_jornada      int      obligatorio
 *   etiqueta_temporada  string   obligatorio
 *   modelo              string   opcional, p.ej. 'elo_v1' (va a `calidad`)
 *   tipo                string   opcional, franja temporal; por defecto T-24
 *   casillas            array    posicion 1..14 y p1/px/p2 en fracciones 0..1
 *
 * La posición 15 no se acepta: el Pleno no se juega a 1/X/2 sino a goles, y
 * tiene su propia tabla.
 *
 * Idempotente: recalcular la misma jornada actualiza en lugar de duplicar.
 */

$numeroJornada = required_int($data, 'numero_jornada');
$etiquetaTemporada = str_required($data, 'etiqueta_temporada', 20);

$modelo = isset($data['modelo']) && $data['modelo'] !== ''
    ? str_required($data, 'modelo', 30)
    : 'modelo_propio';

$TIPOS = ['CIERRE', 'T-2', 'T-24', 'T-72', 'T-72+', 'ESTIMADO'];
$tipo = isset($data['tipo']) && $data['tipo'] !== ''
    ? strtoupper(str_required($data, 'tipo', 20))
    : 'T-24';
if (!in_array($tipo, $TIPOS, true)) {
    api_fail('tipo debe ser uno de: ' . implode(', ', $TIPOS), 422);
}

if (!isset($data['casillas']) || !is_array($data['casillas']) || $data['casillas'] === []) {
    api_fail('casillas debe ser un array con al menos un elemento.', 422);
}
if (count($data['casillas']) > 14) {
    api_fail('casillas no puede tener más de 14 elementos (el Pleno va aparte).', 422);
}

$casillas = [];
$vistas = [];

foreach ($data['casillas'] as $i => $c) {
    if (!is_array($c)) {
        api_fail("casillas[$i] debe ser un objeto.", 422);
    }

    $posicion = required_int($c, 'posicion');
    if ($posicion < 1 || $posicion > 14) {
        api_fail("casillas[$i].posicion fuera de rango (1..14).", 422);
    }
    if (isset($vistas[$posicion])) {
        api_fail("posicion $posicion duplicada.", 422);
    }
    $vistas[$posicion] = true;

    $p = [];
    foreach (['p1', 'px', 'p2'] as $k) {
        if (!isset($c[$k]) || !is_numeric($c[$k])) {
            api_fail("casillas[$i].$k debe ser numérico.", 422);
        }
        $v = (float)$c[$k];
        if ($v < 0 || $v > 1) {
            api_fail("casillas[$i].$k debe estar entre 0 y 1.", 422);
        }
        $p[$k] = $v;
    }

    // Los tres signos son excluyentes y cubren todos los casos: si no suman
    // 1 es que el modelo ha calculado mal, y guardarlo contaminaría después
    // cualquier comparación con el mercado.
    $suma = $p['p1'] + $p['px'] + $p['p2'];
    if (abs($suma - 1.0) > 0.02) {
        api_fail(
            "casillas[$i]: las probabilidades suman " . round($suma, 4)
            . ' y deben sumar 1.',
            422
        );
    }

    $casillas[] = ['posicion' => $posicion] + $p;
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
            . 'Hay que cargarla antes de guardar un pronóstico.',
            422
        );
    }
    $jornadaId = (int)$jornadaId;

    $pdo->beginTransaction();

    $ins = $pdo->prepare("
        INSERT INTO quiniela_probabilidades
            (jornada_id, posicion, fuente, tipo, prob_1, prob_x, prob_2,
             es_estimado, calidad)
        VALUES (:j, :p, 'MODELO_PROPIO', :ti, :p1, :px, :p2, 1, :cal)
        ON DUPLICATE KEY UPDATE
            prob_1 = VALUES(prob_1), prob_x = VALUES(prob_x),
            prob_2 = VALUES(prob_2), calidad = VALUES(calidad)
    ");

    foreach ($casillas as $c) {
        $ins->execute([
            ':j' => $jornadaId, ':p' => $c['posicion'], ':ti' => $tipo,
            ':p1' => $c['p1'], ':px' => $c['px'], ':p2' => $c['p2'],
            ':cal' => $modelo,
        ]);
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'jornada_id' => $jornadaId,
        'casillas' => count($casillas),
        'fuente' => 'MODELO_PROPIO',
        'modelo' => $modelo,
        'tipo' => $tipo,
    ], 201);
} catch (Throwable $e) {
    if (isset($pdo) && $pdo->inTransaction()) {
        $pdo->rollBack();
    }
    error_log('guardar_pronostico.php: ' . $e->getMessage());
    api_fail('No se pudo guardar el pronóstico.', 500);
}
