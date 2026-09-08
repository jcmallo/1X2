<?php
declare(strict_types=1);
require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();
$data = json_body();

$fuente = str_required($data, 'fuente', 50);
$tipoFuente = str_required($data, 'tipo_fuente', 20);
$notas = null;
if (isset($data['notas']) && $data['notas'] !== null) {
    if (!is_string($data['notas']) || strlen($data['notas']) > 2000) {
        api_fail('notas debe ser texto de máximo 2000 caracteres.', 422);
    }
    $notas = trim($data['notas']);
}

try {
    $pdo = db();
    $stmt = $pdo->prepare("
        INSERT INTO bruto_lotes_ingesta
            (fuente, tipo_fuente, iniciado_en, estado, notas)
        VALUES
            (:fuente, :tipo_fuente, NOW(), 'en_curso', :notas)
    ");
    $stmt->execute([
        ':fuente' => $fuente,
        ':tipo_fuente' => $tipoFuente,
        ':notas' => $notas,
    ]);

    api_json([
        'ok' => true,
        'lote_id' => (int)$pdo->lastInsertId(),
    ], 201);
} catch (Throwable $e) {
    error_log('iniciar_lote.php: ' . $e->getMessage());
    api_fail('No se pudo iniciar el lote.', 500);
}
