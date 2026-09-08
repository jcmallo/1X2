<?php
declare(strict_types=1);
require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

$loteId = required_int($data, 'lote_id');
$fuente = str_required($data, 'fuente', 50);
$url = str_required($data, 'url', 500);
$tipo = str_required($data, 'tipo_contenido', 30);
$contenido = str_required($data, 'contenido', 2_000_000);

$obtenidoEn = normalize_datetime(
    isset($data['obtenido_en']) ? (string)$data['obtenido_en'] : null,
    'obtenido_en'
) ?? date('Y-m-d H:i:s');

$hash = hash('sha256', $contenido);

try {
    $pdo = db();

    $check = $pdo->prepare(
        "SELECT lote_id FROM bruto_lotes_ingesta WHERE lote_id = :id"
    );
    $check->execute([':id' => $loteId]);
    if (!$check->fetch()) {
        api_fail('lote_id no existe.', 422);
    }

    $stmt = $pdo->prepare("
        INSERT INTO bruto_documentos
            (lote_id, fuente, url, tipo_contenido, obtenido_en,
             contenido, hash_contenido)
        VALUES
            (:lote_id, :fuente, :url, :tipo, :obtenido_en,
             :contenido, :hash)
    ");
    $stmt->execute([
        ':lote_id' => $loteId,
        ':fuente' => $fuente,
        ':url' => $url,
        ':tipo' => $tipo,
        ':obtenido_en' => $obtenidoEn,
        ':contenido' => $contenido,
        ':hash' => $hash,
    ]);

    api_json([
        'ok' => true,
        'documento_id' => (int)$pdo->lastInsertId(),
        'hash_contenido' => $hash,
    ], 201);

} catch (Throwable $e) {
    error_log('guardar_documento.php: ' . $e->getMessage());
    api_fail('No se pudo guardar el documento RAW.', 500);
}
