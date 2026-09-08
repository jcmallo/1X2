<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

/**
 * Guarda el análisis de la jornada hecho por Claude.
 *
 * Se guarda el texto entero y no un resumen porque el objetivo no es solo
 * enseñar la propuesta: es poder comprobar, dentro de unos meses, si los
 * ajustes de la IA acertaron más que dejar el cálculo estadístico como
 * estaba. Sin el razonamiento escrito no hay forma de juzgarlo.
 *
 * También se guardan los tokens y el coste. Una capa que cuesta dinero en
 * cada ejecución tiene que poder justificarse con números, no con impresiones.
 *
 * Payload:
 *   numero_jornada, etiqueta_temporada   obligatorios
 *   modelo                                p.ej. claude-opus-5
 *   tipo                                  franja temporal, por defecto T-24
 *   analisis                              el texto completo
 *   ajustes                               JSON con los cambios propuestos
 *   senales                               JSON con los hechos encontrados
 *   solicitud_id                          opcional: cierra el candado
 *   tokens_entrada, tokens_salida, coste_usd, busquedas_web
 *
 * Idempotente por (jornada, modelo, tipo): repetir el análisis lo actualiza.
 */

$numeroJornada = required_int($data, 'numero_jornada');
$etiquetaTemporada = str_required($data, 'etiqueta_temporada', 20);
$modelo = str_required($data, 'modelo', 40);

$TIPOS = ['CIERRE', 'T-2', 'T-24', 'T-72', 'T-72+', 'ESTIMADO'];
$tipo = isset($data['tipo']) && $data['tipo'] !== ''
    ? strtoupper(str_required($data, 'tipo', 20))
    : 'T-24';
if (!in_array($tipo, $TIPOS, true)) {
    api_fail('tipo debe ser uno de: ' . implode(', ', $TIPOS), 422);
}

$analisis = isset($data['analisis']) ? (string)$data['analisis'] : '';
if (strlen($analisis) > 200000) {
    api_fail('analisis excede el tamaño razonable (200 KB).', 422);
}

// El id del lanzamiento que pidió este análisis, si vino del botón de la
// web. Se cierra aquí y no en un endpoint aparte para que sea atómico: si el
// análisis se guarda, el candado queda cerrado; si no, sigue abierto y se
// puede reintentar.
$solicitudId = isset($data['solicitud_id']) && $data['solicitud_id'] !== ''
    ? (int)$data['solicitud_id'] : null;

$senales = isset($data['senales']) ? (string)$data['senales'] : null;
if ($senales !== null && $senales !== '') {
    if (strlen($senales) > 200000) {
        api_fail('senales excede el tamaño razonable (200 KB).', 422);
    }
    json_decode($senales);
    if (json_last_error() !== JSON_ERROR_NONE) {
        api_fail('senales debe ser JSON válido: ' . json_last_error_msg(), 422);
    }
}

$ajustes = isset($data['ajustes']) ? (string)$data['ajustes'] : null;
if ($ajustes !== null && $ajustes !== '') {
    // Si no es JSON válido no se guarda a medias: se rechaza, porque un
    // campo con JSON roto es peor que un campo vacío.
    json_decode($ajustes);
    if (json_last_error() !== JSON_ERROR_NONE) {
        api_fail('ajustes debe ser JSON válido: ' . json_last_error_msg(), 422);
    }
}

$combinacion = null;
$pleno = null;
if ($ajustes) {
    $obj = json_decode($ajustes, true);
    if (isset($obj['pleno']['local'], $obj['pleno']['visitante'])) {
        $l = strtoupper(trim((string)$obj['pleno']['local']));
        $v = strtoupper(trim((string)$obj['pleno']['visitante']));
        if (preg_match('/^[012M]$/', $l) && preg_match('/^[012M]$/', $v)) {
            $pleno = $l . '-' . $v;
        }
    }
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
        api_fail("No existe la jornada $numeroJornada de $etiquetaTemporada.", 422);
    }

    $stmt = $pdo->prepare("
        INSERT INTO ia_analisis_jornada
            (jornada_id, modelo, tipo, analisis, ajustes, senales, combinacion, pleno,
             tokens_entrada, tokens_salida, coste_usd, busquedas_web)
        VALUES (:j, :m, :ti, :an, :aj, :se, :co, :pl, :te, :ts, :cu, :bw)
        ON DUPLICATE KEY UPDATE
            analisis = VALUES(analisis), ajustes = VALUES(ajustes),
            senales = VALUES(senales),
            combinacion = VALUES(combinacion), pleno = VALUES(pleno),
            tokens_entrada = VALUES(tokens_entrada),
            tokens_salida = VALUES(tokens_salida),
            coste_usd = VALUES(coste_usd),
            busquedas_web = VALUES(busquedas_web),
            creado_en = NOW()
    ");
    $stmt->execute([
        ':j' => (int)$jornadaId, ':m' => $modelo, ':ti' => $tipo,
        ':an' => $analisis, ':aj' => $ajustes, ':se' => $senales,
        ':co' => $combinacion, ':pl' => $pleno,
        ':te' => nullable_int($data, 'tokens_entrada'),
        ':ts' => nullable_int($data, 'tokens_salida'),
        ':cu' => nullable_float($data, 'coste_usd'),
        ':bw' => nullable_int($data, 'busquedas_web'),
    ]);

    // El candado se cierra solo si el análisis ha llegado a guardarse. Un
    // fallo antes de aquí deja el lanzamiento abierto, y pasados 30 minutos
    // el panel vuelve a permitir intentarlo.
    if ($solicitudId !== null) {
        $pdo->prepare("
            UPDATE ia_lanzamientos
            SET estado = 'hecho', terminado_en = NOW()
            WHERE id = :i
        ")->execute([':i' => $solicitudId]);
    }

    api_json([
        'ok' => true,
        'id' => (int)$pdo->lastInsertId(),
        'jornada_id' => (int)$jornadaId,
        'modelo' => $modelo,
        'pleno' => $pleno,
    ], 201);
} catch (Throwable $e) {
    error_log('guardar_analisis_ia.php: ' . $e->getMessage());
    api_fail('No se pudo guardar el análisis.', 500);
}
