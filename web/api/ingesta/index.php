<?php
declare(strict_types=1);
http_response_code(404);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
echo json_encode([
    'ok' => false,
    'error' => 'Endpoint no disponible.'
], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
