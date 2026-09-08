<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

$loteId = required_int($data, 'lote_id');
$partidoId = required_int($data, 'partido_id');
$estadioId = required_int($data, 'estadio_id');

$tipo = isset($data['tipo']) && is_string($data['tipo'])
    ? trim($data['tipo'])
    : '';

if (!in_array($tipo, ['observacion', 'prevision'], true)) {
    api_fail("tipo debe ser 'observacion' o 'prevision'.", 422);
}

$fuente = str_required($data, 'fuente', 50);
$endpoint = str_required($data, 'endpoint', 255);

$parametros = $data['parametros_solicitud'] ?? null;
$rawPayload = $data['raw_payload'] ?? null;

if ($rawPayload === null) {
    api_fail('raw_payload es obligatorio.', 422);
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

if ($parametros !== null && $parametrosJson === false) {
    api_fail('parametros_solicitud no se pudo serializar.', 422);
}

$rawJson = is_string($rawPayload)
    ? $rawPayload
    : json_encode($rawPayload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

if ($rawJson === false) {
    api_fail('raw_payload no se pudo serializar.', 422);
}

$hashPayload = hash('sha256', $rawJson);

function nf(array $data, string $campo): ?float {
    return nullable_float($data, $campo);
}

try {
    $pdo = db();
    $pdo->beginTransaction();

    // Validar referencias principales.
    $stmt = $pdo->prepare("
        SELECT partido_id, estadio_id
        FROM nucleo_partidos
        WHERE partido_id = :partido_id
        LIMIT 1
        FOR UPDATE
    ");
    $stmt->execute([':partido_id' => $partidoId]);
    $partido = $stmt->fetch();

    if (!$partido) {
        throw new RuntimeException('PARTIDO_NO_EXISTE');
    }

    if ((int)$partido['estadio_id'] !== $estadioId) {
        throw new RuntimeException('ESTADIO_NO_COINCIDE');
    }

    $stmt = $pdo->prepare("
        SELECT lote_id
        FROM bruto_lotes_ingesta
        WHERE lote_id = :lote_id
        LIMIT 1
    ");
    $stmt->execute([':lote_id' => $loteId]);
    if (!$stmt->fetch()) {
        throw new RuntimeException('LOTE_NO_EXISTE');
    }

    // RAW idempotente: si la respuesta exacta ya está guardada,
    // reutilizamos su respuesta_id en vez de duplicarla.
    $stmt = $pdo->prepare("
        SELECT respuesta_id
        FROM bruto_respuestas_api
        WHERE fuente = :fuente
          AND endpoint = :endpoint
          AND hash_payload = :hash_payload
        ORDER BY respuesta_id ASC
        LIMIT 1
    ");
    $stmt->execute([
        ':fuente' => $fuente,
        ':endpoint' => $endpoint,
        ':hash_payload' => $hashPayload,
    ]);
    $rawExistente = $stmt->fetch();

    if ($rawExistente) {
        $respuestaRawId = (int)$rawExistente['respuesta_id'];
    } else {
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
        $respuestaRawId = (int)$pdo->lastInsertId();
    }

    if ($tipo === 'observacion') {
        $observadoEn = normalize_datetime(
            isset($data['observado_en']) ? (string)$data['observado_en'] : null,
            'observado_en'
        );
        if ($observadoEn === null) {
            api_fail('observado_en es obligatorio.', 422);
        }

        $stmt = $pdo->prepare("
            SELECT id
            FROM clima_observaciones
            WHERE partido_id = :partido_id
              AND fuente = :fuente
            ORDER BY id ASC
            LIMIT 1
            FOR UPDATE
        ");
        $stmt->execute([
            ':partido_id' => $partidoId,
            ':fuente' => $fuente,
        ]);
        $existente = $stmt->fetch();

        $valores = [
            ':estadio_id' => $estadioId,
            ':partido_id' => $partidoId,
            ':observado_en' => $observadoEn,
            ':temperatura_c' => nf($data, 'temperatura_c'),
            ':lluvia_mm' => nf($data, 'lluvia_mm'),
            ':humedad_pct' => nf($data, 'humedad_pct'),
            ':velocidad_viento_kmh' => nf($data, 'velocidad_viento_kmh'),
            ':fuente' => $fuente,
            ':origen_respuesta_id' => $respuestaRawId,
        ];

        if ($existente) {
            $stmt = $pdo->prepare("
                UPDATE clima_observaciones
                SET estadio_id = :estadio_id,
                    observado_en = :observado_en,
                    temperatura_c = :temperatura_c,
                    lluvia_mm = :lluvia_mm,
                    humedad_pct = :humedad_pct,
                    velocidad_viento_kmh = :velocidad_viento_kmh,
                    origen_respuesta_id = :origen_respuesta_id
                WHERE id = :id
            ");
            $valores[':id'] = (int)$existente['id'];
            $stmt->execute($valores);
            $registroId = (int)$existente['id'];
            $accion = 'actualizado';
        } else {
            $stmt = $pdo->prepare("
                INSERT INTO clima_observaciones
                    (estadio_id, partido_id, observado_en,
                     temperatura_c, lluvia_mm, humedad_pct,
                     velocidad_viento_kmh, fuente, origen_respuesta_id)
                VALUES
                    (:estadio_id, :partido_id, :observado_en,
                     :temperatura_c, :lluvia_mm, :humedad_pct,
                     :velocidad_viento_kmh, :fuente, :origen_respuesta_id)
            ");
            $stmt->execute($valores);
            $registroId = (int)$pdo->lastInsertId();
            $accion = 'creado';
        }

        $pdo->commit();

        api_json([
            'ok' => true,
            'tipo' => 'observacion',
            'accion' => $accion,
            'registro_id' => $registroId,
            'respuesta_raw_id' => $respuestaRawId,
        ]);
    }

    // PREVISIÓN histórica reconstruida a lead time fijo.
    $previsionGeneradaEn = normalize_datetime(
        isset($data['prevision_generada_en'])
            ? (string)$data['prevision_generada_en']
            : null,
        'prevision_generada_en'
    );
    $previstaPara = normalize_datetime(
        isset($data['prevista_para']) ? (string)$data['prevista_para'] : null,
        'prevista_para'
    );
    $horasAntelacion = nullable_int($data, 'horas_antelacion');

    if ($previsionGeneradaEn === null || $previstaPara === null) {
        api_fail(
            'prevision_generada_en y prevista_para son obligatorios.',
            422
        );
    }
    if (!in_array($horasAntelacion, [24, 72], true)) {
        api_fail('horas_antelacion debe ser 24 o 72.', 422);
    }

    $stmt = $pdo->prepare("
        SELECT id
        FROM clima_previsiones
        WHERE partido_id = :partido_id
          AND fuente = :fuente
          AND horas_antelacion = :horas_antelacion
        ORDER BY id ASC
        LIMIT 1
        FOR UPDATE
    ");
    $stmt->execute([
        ':partido_id' => $partidoId,
        ':fuente' => $fuente,
        ':horas_antelacion' => $horasAntelacion,
    ]);
    $existente = $stmt->fetch();

    $valores = [
        ':estadio_id' => $estadioId,
        ':partido_id' => $partidoId,
        ':prevision_generada_en' => $previsionGeneradaEn,
        ':prevista_para' => $previstaPara,
        ':horas_antelacion' => $horasAntelacion,
        ':temperatura_c' => nf($data, 'temperatura_c'),
        ':sensacion_termica_c' => nf($data, 'sensacion_termica_c'),
        ':lluvia_mm' => nf($data, 'lluvia_mm'),
        ':probabilidad_lluvia_pct' => nf($data, 'probabilidad_lluvia_pct'),
        ':humedad_pct' => nf($data, 'humedad_pct'),
        ':velocidad_viento_kmh' => nf($data, 'velocidad_viento_kmh'),
        ':rachas_viento_kmh' => nf($data, 'rachas_viento_kmh'),
        ':presion_hpa' => nf($data, 'presion_hpa'),
        ':nubosidad_pct' => nf($data, 'nubosidad_pct'),
        ':visibilidad_km' => nf($data, 'visibilidad_km'),
        ':nieve_mm' => nf($data, 'nieve_mm'),
        ':fuente' => $fuente,
        ':origen_respuesta_id' => $respuestaRawId,
    ];

    if ($existente) {
        $stmt = $pdo->prepare("
            UPDATE clima_previsiones
            SET estadio_id = :estadio_id,
                prevision_generada_en = :prevision_generada_en,
                prevista_para = :prevista_para,
                temperatura_c = :temperatura_c,
                sensacion_termica_c = :sensacion_termica_c,
                lluvia_mm = :lluvia_mm,
                probabilidad_lluvia_pct = :probabilidad_lluvia_pct,
                humedad_pct = :humedad_pct,
                velocidad_viento_kmh = :velocidad_viento_kmh,
                rachas_viento_kmh = :rachas_viento_kmh,
                presion_hpa = :presion_hpa,
                nubosidad_pct = :nubosidad_pct,
                visibilidad_km = :visibilidad_km,
                nieve_mm = :nieve_mm,
                origen_respuesta_id = :origen_respuesta_id
            WHERE id = :id
        ");
        $valores[':id'] = (int)$existente['id'];
        $stmt->execute($valores);
        $registroId = (int)$existente['id'];
        $accion = 'actualizado';
    } else {
        $stmt = $pdo->prepare("
            INSERT INTO clima_previsiones
                (estadio_id, partido_id, prevision_generada_en, prevista_para,
                 horas_antelacion, temperatura_c, sensacion_termica_c,
                 lluvia_mm, probabilidad_lluvia_pct, humedad_pct,
                 velocidad_viento_kmh, rachas_viento_kmh, presion_hpa,
                 nubosidad_pct, visibilidad_km, nieve_mm,
                 fuente, origen_respuesta_id)
            VALUES
                (:estadio_id, :partido_id, :prevision_generada_en, :prevista_para,
                 :horas_antelacion, :temperatura_c, :sensacion_termica_c,
                 :lluvia_mm, :probabilidad_lluvia_pct, :humedad_pct,
                 :velocidad_viento_kmh, :rachas_viento_kmh, :presion_hpa,
                 :nubosidad_pct, :visibilidad_km, :nieve_mm,
                 :fuente, :origen_respuesta_id)
        ");
        $stmt->execute($valores);
        $registroId = (int)$pdo->lastInsertId();
        $accion = 'creado';
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'tipo' => 'prevision',
        'accion' => $accion,
        'registro_id' => $registroId,
        'respuesta_raw_id' => $respuestaRawId,
        'horas_antelacion' => $horasAntelacion,
    ]);

} catch (Throwable $e) {
    if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) {
        $pdo->rollBack();
    }

    if ($e->getMessage() === 'PARTIDO_NO_EXISTE') {
        api_fail('partido_id no existe.', 422);
    }
    if ($e->getMessage() === 'ESTADIO_NO_COINCIDE') {
        api_fail('estadio_id no coincide con el partido.', 422);
    }
    if ($e->getMessage() === 'LOTE_NO_EXISTE') {
        api_fail('lote_id no existe.', 422);
    }

    error_log('guardar_clima_historico.php: ' . $e->getMessage());
    api_fail('No se pudo guardar el clima histórico.', 500);
}
