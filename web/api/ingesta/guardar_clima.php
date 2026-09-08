<?php
declare(strict_types=1);
require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();
$data = json_body();

$loteId = required_int($data, 'lote_id');
$estadioId = required_int($data, 'estadio_id');
$partidoId = nullable_int($data, 'partido_id');

$previsionGenerada = normalize_datetime(
    isset($data['prevision_generada_en']) ? (string)$data['prevision_generada_en'] : null,
    'prevision_generada_en'
);
$previstaPara = normalize_datetime(
    isset($data['prevista_para']) ? (string)$data['prevista_para'] : null,
    'prevista_para'
);

if ($previsionGenerada === null || $previstaPara === null) {
    api_fail('prevision_generada_en y prevista_para son obligatorios.', 422);
}

$horasAntelacion = nullable_int($data, 'horas_antelacion');
if ($horasAntelacion !== null && ($horasAntelacion < 0 || $horasAntelacion > 1000)) {
    api_fail('horas_antelacion fuera de rango.', 422);
}

$fuente = isset($data['fuente']) && is_string($data['fuente'])
    ? trim($data['fuente'])
    : 'open-meteo';
if ($fuente === '' || strlen($fuente) > 50) {
    api_fail('fuente inválida.', 422);
}

$endpoint = isset($data['endpoint']) && is_string($data['endpoint'])
    ? trim($data['endpoint'])
    : 'open-meteo';
if ($endpoint === '' || strlen($endpoint) > 255) {
    api_fail('endpoint inválido.', 422);
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

$parametros = $data['parametros_solicitud'] ?? null;
$rawPayload = $data['raw_payload'] ?? ($data['raw'] ?? null);

if ($rawPayload === null) {
    api_fail('Falta raw_payload (respuesta cruda de Open-Meteo).', 422);
}

$parametrosJson = $parametros === null
    ? null
    : json_encode($parametros, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

$rawJson = is_string($rawPayload)
    ? $rawPayload
    : json_encode($rawPayload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

if ($rawJson === false || json_decode($rawJson, true) === null && strtolower(trim($rawJson)) !== 'null') {
    api_fail('raw_payload no es JSON serializable/válido.', 422);
}

$hashPayload = hash('sha256', $rawJson);

try {
    $pdo = db();
    $pdo->beginTransaction();

    // Validar referencias antes de insertar.
    $check = $pdo->prepare("SELECT estadio_id FROM nucleo_estadios WHERE estadio_id = :id");
    $check->execute([':id' => $estadioId]);
    if (!$check->fetch()) {
        throw new RuntimeException('ESTADIO_NO_EXISTE');
    }

    if ($partidoId !== null) {
        $check = $pdo->prepare("SELECT partido_id FROM nucleo_partidos WHERE partido_id = :id");
        $check->execute([':id' => $partidoId]);
        if (!$check->fetch()) {
            throw new RuntimeException('PARTIDO_NO_EXISTE');
        }
    }

    $check = $pdo->prepare("SELECT lote_id FROM bruto_lotes_ingesta WHERE lote_id = :id");
    $check->execute([':id' => $loteId]);
    if (!$check->fetch()) {
        throw new RuntimeException('LOTE_NO_EXISTE');
    }

    // 1) RAW exacto.
    $stmt = $pdo->prepare("
        INSERT INTO bruto_respuestas_api
            (lote_id, fuente, endpoint, parametros_solicitud,
             solicitado_en, respondido_en, codigo_http, payload, hash_payload)
        VALUES
            (:lote_id, :fuente, :endpoint, :parametros,
             :solicitado_en, :respondido_en, :codigo_http, :payload, :hash_payload)
    ");
    $stmt->execute([
        ':lote_id' => $loteId,
        ':fuente' => $fuente,
        ':endpoint' => $endpoint,
        ':parametros' => $parametrosJson,
        ':solicitado_en' => $solicitadoEn,
        ':respondido_en' => $respondidoEn,
        ':codigo_http' => $codigoHttp,
        ':payload' => $rawJson,
        ':hash_payload' => $hashPayload,
    ]);
    $respuestaId = (int)$pdo->lastInsertId();

    // 2) Capa limpia.
    $stmt = $pdo->prepare("
        INSERT INTO clima_previsiones
            (estadio_id, partido_id, prevision_generada_en, prevista_para,
             horas_antelacion, temperatura_c, sensacion_termica_c, lluvia_mm,
             probabilidad_lluvia_pct, humedad_pct, velocidad_viento_kmh,
             rachas_viento_kmh, presion_hpa, nubosidad_pct, visibilidad_km,
             nieve_mm, fuente, origen_respuesta_id)
        VALUES
            (:estadio_id, :partido_id, :prevision_generada_en, :prevista_para,
             :horas_antelacion, :temperatura_c, :sensacion_termica_c, :lluvia_mm,
             :probabilidad_lluvia_pct, :humedad_pct, :velocidad_viento_kmh,
             :rachas_viento_kmh, :presion_hpa, :nubosidad_pct, :visibilidad_km,
             :nieve_mm, :fuente, :origen_respuesta_id)
    ");
    $stmt->execute([
        ':estadio_id' => $estadioId,
        ':partido_id' => $partidoId,
        ':prevision_generada_en' => $previsionGenerada,
        ':prevista_para' => $previstaPara,
        ':horas_antelacion' => $horasAntelacion,
        ':temperatura_c' => nullable_float($data, 'temperatura_c'),
        ':sensacion_termica_c' => nullable_float($data, 'sensacion_termica_c'),
        ':lluvia_mm' => nullable_float($data, 'lluvia_mm'),
        ':probabilidad_lluvia_pct' => nullable_float($data, 'probabilidad_lluvia_pct'),
        ':humedad_pct' => nullable_float($data, 'humedad_pct'),
        ':velocidad_viento_kmh' => nullable_float($data, 'velocidad_viento_kmh'),
        ':rachas_viento_kmh' => nullable_float($data, 'rachas_viento_kmh'),
        ':presion_hpa' => nullable_float($data, 'presion_hpa'),
        ':nubosidad_pct' => nullable_float($data, 'nubosidad_pct'),
        ':visibilidad_km' => nullable_float($data, 'visibilidad_km'),
        ':nieve_mm' => nullable_float($data, 'nieve_mm'),
        ':fuente' => $fuente,
        ':origen_respuesta_id' => $respuestaId,
    ]);
    $previsionId = (int)$pdo->lastInsertId();

    $pdo->commit();

    api_json([
        'ok' => true,
        'respuesta_raw_id' => $respuestaId,
        'prevision_id' => $previsionId,
        'hash_payload' => $hashPayload,
    ], 201);

} catch (Throwable $e) {
    if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) {
        $pdo->rollBack();
    }

    if ($e->getMessage() === 'ESTADIO_NO_EXISTE') {
        api_fail('estadio_id no existe.', 422);
    }
    if ($e->getMessage() === 'PARTIDO_NO_EXISTE') {
        api_fail('partido_id no existe.', 422);
    }
    if ($e->getMessage() === 'LOTE_NO_EXISTE') {
        api_fail('lote_id no existe.', 422);
    }

    error_log('guardar_clima.php: ' . $e->getMessage());
    api_fail('No se pudo guardar la previsión.', 500);
}
