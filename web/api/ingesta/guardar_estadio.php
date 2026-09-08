<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

$loteId = required_int($data, 'lote_id');
$estadioId = required_int($data, 'estadio_id');

$latitud = nullable_float($data, 'latitud');
$longitud = nullable_float($data, 'longitud');

if ($latitud === null || $longitud === null) {
    api_fail('latitud y longitud son obligatorias.', 422);
}
if ($latitud < -90 || $latitud > 90) {
    api_fail('latitud fuera de rango.', 422);
}
if ($longitud < -180 || $longitud > 180) {
    api_fail('longitud fuera de rango.', 422);
}

$ciudad = null;
if (isset($data['ciudad']) && $data['ciudad'] !== null) {
    if (!is_string($data['ciudad'])) {
        api_fail('ciudad inválida.', 422);
    }
    $ciudad = trim($data['ciudad']);
    if ($ciudad === '') {
        $ciudad = null;
    }
    if ($ciudad !== null && strlen($ciudad) > 100) {
        api_fail('ciudad supera 100 caracteres.', 422);
    }
}

$fuenteCoordenadas = str_required($data, 'fuente_coordenadas', 500);

$notasCalidad = null;
if (isset($data['notas_calidad']) && $data['notas_calidad'] !== null) {
    if (!is_string($data['notas_calidad'])) {
        api_fail('notas_calidad inválida.', 422);
    }
    $notasCalidad = trim($data['notas_calidad']);
    if ($notasCalidad === '') {
        $notasCalidad = null;
    }
    if ($notasCalidad !== null && strlen($notasCalidad) > 500) {
        api_fail('notas_calidad supera 500 caracteres.', 422);
    }
}

$verificadas = isset($data['coordenadas_verificadas'])
    ? ((int)$data['coordenadas_verificadas'] ? 1 : 0)
    : 0;

$sobrescribir = isset($data['sobrescribir'])
    ? ((int)$data['sobrescribir'] ? 1 : 0)
    : 0;

$fuenteRaw = isset($data['fuente_raw']) && is_string($data['fuente_raw'])
    ? trim($data['fuente_raw'])
    : 'nominatim';

$endpoint = isset($data['endpoint']) && is_string($data['endpoint'])
    ? trim($data['endpoint'])
    : 'https://nominatim.openstreetmap.org/search';

$parametros = $data['parametros_solicitud'] ?? null;
$rawPayload = $data['raw_payload'] ?? null;

if ($rawPayload === null) {
    api_fail('raw_payload es obligatorio para conservar trazabilidad.', 422);
}

$solicitadoEn = normalize_datetime(
    isset($data['solicitado_en']) ? (string)$data['solicitado_en'] : null,
    'solicitado_en'
) ?? date('Y-m-d H:i:s');

$respondidoEn = normalize_datetime(
    isset($data['respondido_en']) ? (string)$data['respondido_en'] : null,
    'respondido_en'
) ?? date('Y-m-d H:i:s');

$codigoHttp = nullable_int($data, 'codigo_http') ?? 200;

$parametrosJson = $parametros === null
    ? null
    : json_encode($parametros, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

$rawJson = is_string($rawPayload)
    ? $rawPayload
    : json_encode($rawPayload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

if ($rawJson === false) {
    api_fail('raw_payload no se pudo serializar.', 422);
}

$hashPayload = hash('sha256', $rawJson);

try {
    $pdo = db();
    $pdo->beginTransaction();

    $stmt = $pdo->prepare("
        SELECT estadio_id, latitud, longitud
        FROM nucleo_estadios
        WHERE estadio_id = :id
        FOR UPDATE
    ");
    $stmt->execute([':id' => $estadioId]);
    $estadio = $stmt->fetch();

    if (!$estadio) {
        throw new RuntimeException('ESTADIO_NO_EXISTE');
    }

    $stmt = $pdo->prepare("
        SELECT lote_id
        FROM bruto_lotes_ingesta
        WHERE lote_id = :id
        LIMIT 1
    ");
    $stmt->execute([':id' => $loteId]);
    if (!$stmt->fetch()) {
        throw new RuntimeException('LOTE_NO_EXISTE');
    }

    // Guardar RAW exacto de la geocodificación.
    $stmt = $pdo->prepare("
        INSERT INTO bruto_respuestas_api
            (lote_id, fuente, endpoint, parametros_solicitud,
             solicitado_en, respondido_en, codigo_http, payload, hash_payload)
        VALUES
            (:lote_id, :fuente, :endpoint, :parametros,
             :solicitado_en, :respondido_en, :codigo_http, :payload, :hash)
    ");
    $stmt->execute([
        ':lote_id' => $loteId,
        ':fuente' => $fuenteRaw,
        ':endpoint' => $endpoint,
        ':parametros' => $parametrosJson,
        ':solicitado_en' => $solicitadoEn,
        ':respondido_en' => $respondidoEn,
        ':codigo_http' => $codigoHttp,
        ':payload' => $rawJson,
        ':hash' => $hashPayload,
    ]);
    $respuestaRawId = (int)$pdo->lastInsertId();

    $yaTieneCoordenadas = (
        $estadio['latitud'] !== null
        && $estadio['longitud'] !== null
    );

    if ($yaTieneCoordenadas && !$sobrescribir) {
        $pdo->commit();

        api_json([
            'ok' => true,
            'accion' => 'sin_cambios',
            'estadio_id' => $estadioId,
            'respuesta_raw_id' => $respuestaRawId,
            'motivo' => 'El estadio ya tenía coordenadas y sobrescribir=0.',
        ]);
    }

    $stmt = $pdo->prepare("
        UPDATE nucleo_estadios
        SET ciudad = COALESCE(:ciudad, ciudad),
            latitud = :latitud,
            longitud = :longitud,
            coordenadas_verificadas = :verificadas,
            fuente_coordenadas = :fuente_coordenadas,
            notas_calidad = :notas_calidad
        WHERE estadio_id = :estadio_id
    ");
    $stmt->execute([
        ':ciudad' => $ciudad,
        ':latitud' => $latitud,
        ':longitud' => $longitud,
        ':verificadas' => $verificadas,
        ':fuente_coordenadas' => $fuenteCoordenadas,
        ':notas_calidad' => $notasCalidad,
        ':estadio_id' => $estadioId,
    ]);

    $pdo->commit();

    api_json([
        'ok' => true,
        'accion' => 'actualizado',
        'estadio_id' => $estadioId,
        'respuesta_raw_id' => $respuestaRawId,
        'latitud' => $latitud,
        'longitud' => $longitud,
        'ciudad' => $ciudad,
        'coordenadas_verificadas' => $verificadas,
    ]);

} catch (Throwable $e) {
    if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) {
        $pdo->rollBack();
    }

    if ($e->getMessage() === 'ESTADIO_NO_EXISTE') {
        api_fail('estadio_id no existe.', 422);
    }
    if ($e->getMessage() === 'LOTE_NO_EXISTE') {
        api_fail('lote_id no existe.', 422);
    }

    error_log('guardar_estadio.php: ' . $e->getMessage());
    api_fail('No se pudo actualizar el estadio.', 500);
}
