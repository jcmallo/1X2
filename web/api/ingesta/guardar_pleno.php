<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

/**
 * Guarda una captura del Pleno al 15 de cualquier fuente.
 *
 * Por qué hace falta
 * ------------------
 *
 * importar_jornada_historica.php escribe el Pleno, pero solo bajo dos
 * etiquetas: lo que juega el público (LAE) o el modelo propio. No hay forma
 * de decir "esto viene del mercado de apuestas", y mezclarlo con las otras
 * dos impediría después comparar cuál acierta más, que es justo lo que
 * interesa medir.
 *
 * Payload:
 *   numero_jornada      int     obligatorio
 *   etiqueta_temporada  string  obligatorio
 *   fuente              string  obligatorio: MERCADO | MODELO_PROPIO | ...
 *   tipo                string  opcional, franja temporal; por defecto T-24
 *   calidad             string  opcional, de dónde sale (p.ej. 'sportytrader')
 *   local               {p0,p1,p2,pm}  fracciones que suman 1
 *   visitante           {p0,p1,p2,pm}
 *
 * Las cuatro categorías son los goles de cada equipo: 0, 1, 2 o M (tres o
 * más). Son excluyentes y cubren todos los casos, así que tienen que sumar 1.
 *
 * Idempotente por (jornada, fuente, tipo, lado).
 */

$numeroJornada = required_int($data, 'numero_jornada');
$etiquetaTemporada = str_required($data, 'etiqueta_temporada', 20);
$fuente = strtoupper(str_required($data, 'fuente', 30));

$FUENTES = ['MERCADO', 'MODELO_PROPIO', 'LAE_CIERRE', 'LAE_ESTIMADO'];
if (!in_array($fuente, $FUENTES, true)) {
    api_fail('fuente debe ser una de: ' . implode(', ', $FUENTES), 422);
}

$TIPOS = ['CIERRE', 'T-2', 'T-24', 'T-72', 'T-72+', 'ESTIMADO'];
$tipo = isset($data['tipo']) && $data['tipo'] !== ''
    ? strtoupper(str_required($data, 'tipo', 20))
    : 'T-24';
if (!in_array($tipo, $TIPOS, true)) {
    api_fail('tipo debe ser uno de: ' . implode(', ', $TIPOS), 422);
}

$calidad = isset($data['calidad']) && $data['calidad'] !== ''
    ? str_required($data, 'calidad', 30)
    : null;

/** Valida un lado del Pleno y devuelve sus cuatro probabilidades. */
function lado_pleno(array $data, string $clave): array
{
    if (!isset($data[$clave]) || !is_array($data[$clave])) {
        api_fail("$clave es obligatorio y debe ser un objeto.", 422);
    }
    $v = [];
    foreach (['p0', 'p1', 'p2', 'pm'] as $k) {
        if (!isset($data[$clave][$k]) || !is_numeric($data[$clave][$k])) {
            api_fail("$clave.$k debe ser numérico.", 422);
        }
        $x = (float)$data[$clave][$k];
        if ($x < 0 || $x > 1) {
            api_fail("$clave.$k debe estar entre 0 y 1.", 422);
        }
        $v[$k] = $x;
    }
    $suma = array_sum($v);
    if (abs($suma - 1.0) > 0.02) {
        api_fail(
            "$clave: las probabilidades suman " . round($suma, 4)
            . ' y deben sumar 1. Un Pleno mal calculado contamina la '
            . 'categoría de premio más alta del boleto.',
            422
        );
    }
    return $v;
}

$lados = [
    'LOCAL' => lado_pleno($data, 'local'),
    'VISITANTE' => lado_pleno($data, 'visitante'),
];

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
            "No existe la jornada $numeroJornada de $etiquetaTemporada.",
            422
        );
    }

    $pdo->beginTransaction();

    $ins = $pdo->prepare("
        INSERT INTO quiniela_pleno15
            (jornada_id, fuente, tipo, lado, prob_0, prob_1, prob_2, prob_m,
             capturado_en, es_estimado, calidad)
        VALUES (:j, :fu, :ti, :lado, :p0, :p1, :p2, :pm, NOW(), :est, :cal)
        ON DUPLICATE KEY UPDATE
            prob_0 = VALUES(prob_0), prob_1 = VALUES(prob_1),
            prob_2 = VALUES(prob_2), prob_m = VALUES(prob_m),
            capturado_en = VALUES(capturado_en), calidad = VALUES(calidad)
    ");

    foreach ($lados as $lado => $v) {
        $ins->execute([
            ':j' => (int)$jornadaId, ':fu' => $fuente, ':ti' => $tipo,
            ':lado' => $lado,
            ':p0' => $v['p0'], ':p1' => $v['p1'],
            ':p2' => $v['p2'], ':pm' => $v['pm'],
            ':est' => $tipo === 'CIERRE' ? 0 : 1,
            ':cal' => $calidad,
        ]);
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'jornada_id' => (int)$jornadaId,
        'fuente' => $fuente,
        'tipo' => $tipo,
        'lados' => 2,
    ], 201);
} catch (Throwable $e) {
    if (isset($pdo) && $pdo->inTransaction()) {
        $pdo->rollBack();
    }
    error_log('guardar_pleno.php: ' . $e->getMessage());
    api_fail('No se pudo guardar el Pleno.', 500);
}
