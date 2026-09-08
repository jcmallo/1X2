<?php
declare(strict_types=1);
require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

$loteId = required_int($data, 'lote_id');

$estado = isset($data['estado']) && is_string($data['estado'])
    ? trim($data['estado'])
    : 'completado';

$permitidos = ['completado', 'parcial', 'error'];

if (!in_array($estado, $permitidos, true)) {
    api_fail('estado inválido.', 422);
}

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
        UPDATE bruto_lotes_ingesta
        SET
            finalizado_en = NOW(),
            estado = :estado,
            notas = CASE
                WHEN :guardar_notas = 0 THEN notas
                ELSE CONCAT_WS(
                    '\n',
                    NULLIF(notas, ''),
                    :notas
                )
            END
        WHERE lote_id = :lote_id
    ");

    $stmt->execute([
        ':estado' => $estado,
        ':guardar_notas' => ($notas !== null && $notas !== '') ? 1 : 0,
        ':notas' => $notas,
        ':lote_id' => $loteId,
    ]);

    $check = $pdo->prepare("
        SELECT lote_id
        FROM bruto_lotes_ingesta
        WHERE lote_id = :id
    ");

    $check->execute([':id' => $loteId]);

    if (!$check->fetch()) {
        api_fail('lote_id no existe.', 404);
    }

    api_json([
        'ok' => true,
        'lote_id' => $loteId,
        'estado' => $estado
    ]);

} catch (Throwable $e) {
    error_log('finalizar_lote.php: ' . $e->getMessage());
    api_fail('No se pudo finalizar el lote.', 500);
}
