<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

/**
 * Guarda una captura de cuotas 1X2 en mercado_cuotas_capturadas.
 *
 * Payload:
 *   partido_id          int      obligatorio
 *   casa_apuestas       string   obligatorio (ej. 'betfair_ex_eu')
 *   mercado             string   opcional, por defecto '1X2'
 *   capturado_en        datetime obligatorio
 *   franja_temporal     string   opcional ('T-72','T-24','T-2','CIERRE')
 *   cuota_local         float    opcional
 *   cuota_empate        float    opcional
 *   cuota_visitante     float    opcional
 *   prob_mercado_local     float opcional (0..1)
 *   prob_mercado_empate    float opcional (0..1)
 *   prob_mercado_visitante float opcional (0..1)
 *   fuente              string   obligatorio
 *   origen_respuesta_id int      opcional (FK a bruto_respuestas_api)
 *
 * Reglas:
 *   - Hay que enviar cuotas o probabilidades. Si solo llegan cuotas, las
 *     probabilidades se derivan normalizando el inverso (se elimina el margen).
 *   - Solo se crea fila nueva si la cuota o la franja han cambiado respecto a
 *     la última captura de ese partido, casa y mercado. Ver el bloque de
 *     guardado para el porqué.
 */

$partidoId    = required_int($data, 'partido_id');
$casaApuestas = str_required($data, 'casa_apuestas', 80);
$fuente       = str_required($data, 'fuente', 50);

$mercado = isset($data['mercado']) && $data['mercado'] !== null && $data['mercado'] !== ''
    ? str_required($data, 'mercado', 20)
    : '1X2';

$capturadoEn = normalize_datetime(
    isset($data['capturado_en']) ? (string)$data['capturado_en'] : null,
    'capturado_en'
);
if ($capturadoEn === null) {
    api_fail('capturado_en es obligatorio.', 422);
}

$franjaTemporal = null;
if (isset($data['franja_temporal']) && $data['franja_temporal'] !== null && $data['franja_temporal'] !== '') {
    $franjaTemporal = str_required($data, 'franja_temporal', 10);
}

$origenRespuestaId = nullable_int($data, 'origen_respuesta_id');

$cuotaLocal     = nullable_float($data, 'cuota_local');
$cuotaEmpate    = nullable_float($data, 'cuota_empate');
$cuotaVisitante = nullable_float($data, 'cuota_visitante');

$probLocal     = nullable_float($data, 'prob_mercado_local');
$probEmpate    = nullable_float($data, 'prob_mercado_empate');
$probVisitante = nullable_float($data, 'prob_mercado_visitante');

// --- Validación del partido ---------------------------------------------

$stmt = db()->prepare(
    'SELECT partido_id FROM nucleo_partidos WHERE partido_id = :id LIMIT 1'
);
$stmt->execute([':id' => $partidoId]);
if ($stmt->fetchColumn() === false) {
    api_fail("El partido_id $partidoId no existe en nucleo_partidos.", 422);
}

// --- Cuotas: validación --------------------------------------------------

$tieneCuotas = $cuotaLocal !== null && $cuotaEmpate !== null && $cuotaVisitante !== null;
$tieneProbs  = $probLocal !== null && $probEmpate !== null && $probVisitante !== null;

if (!$tieneCuotas && !$tieneProbs) {
    api_fail('Hay que enviar las tres cuotas o las tres probabilidades.', 422);
}

if ($tieneCuotas) {
    foreach ([
        'cuota_local' => $cuotaLocal,
        'cuota_empate' => $cuotaEmpate,
        'cuota_visitante' => $cuotaVisitante,
    ] as $campo => $valor) {
        if ($valor <= 1.0) {
            api_fail("$campo debe ser mayor que 1.0 (recibido: $valor).", 422);
        }
        if ($valor > 999.0) {
            api_fail("$campo fuera de rango razonable (recibido: $valor).", 422);
        }
    }
}

// --- Probabilidades: derivar o validar -----------------------------------

$overround = null;

if (!$tieneProbs && $tieneCuotas) {
    // Normalizar el inverso de las cuotas: p_i = (1/c_i) / SUM(1/c_j)
    $invLocal     = 1.0 / $cuotaLocal;
    $invEmpate    = 1.0 / $cuotaEmpate;
    $invVisitante = 1.0 / $cuotaVisitante;

    $suma = $invLocal + $invEmpate + $invVisitante;
    if ($suma <= 0) {
        api_fail('Cuotas inconsistentes: no se pueden normalizar.', 422);
    }

    $overround = $suma - 1.0;

    $probLocal     = $invLocal / $suma;
    $probEmpate    = $invEmpate / $suma;
    $probVisitante = $invVisitante / $suma;
} elseif ($tieneProbs) {
    foreach ([
        'prob_mercado_local' => $probLocal,
        'prob_mercado_empate' => $probEmpate,
        'prob_mercado_visitante' => $probVisitante,
    ] as $campo => $valor) {
        if ($valor < 0.0 || $valor > 1.0) {
            api_fail("$campo debe estar entre 0 y 1 (recibido: $valor).", 422);
        }
    }

    $sumaProbs = $probLocal + $probEmpate + $probVisitante;
    if ($sumaProbs < 0.97 || $sumaProbs > 1.03) {
        api_fail(
            'Las probabilidades deben sumar aproximadamente 1 '
            . '(suma recibida: ' . round($sumaProbs, 4) . ').',
            422
        );
    }
}

/**
 * Normaliza una cuota a la escala de la columna DECIMAL(6,3) para poder
 * compararla sin sustos de coma flotante: 1.85 y 1.850 son la misma cuota.
 */
function fmt_cuota($valor): string
{
    if ($valor === null || $valor === '') {
        return '';
    }
    return number_format((float)$valor, 3, '.', '');
}

// --- Guardado ------------------------------------------------------------
//
// La idempotencia NO puede basarse en el instante de la captura.
//
// Se comprobó empíricamente que 'last_update' de The Odds API cambia en 16 de
// cada 20 casas en 45 segundos mientras la cuota permanece idéntica: ese campo
// refleja cuándo el proveedor refrescó su copia, no cuándo la casa movió el
// precio. Usar cualquier reloj como parte de la clave hace que cada ejecución
// duplique la tabla entera.
//
// Por eso lo que decide si hay fila nueva es el DATO: se compara con la última
// captura de ese partido, casa y mercado, y solo se inserta si cambian las
// cuotas o cambia la franja temporal. La tabla queda como una serie de
// cambios reales de precio, no como un registro de cuántas veces preguntamos.
//
// La franja entra en la comparación a propósito: garantiza al menos un punto
// por tramo (T-72, T-24, T-2, CIERRE) aunque el precio no se haya movido, que
// es lo que necesita el modelo para respetar las reglas anti data-leakage.

try {
    $pdo = db();

    $stmt = $pdo->prepare("
        SELECT id, cuota_local, cuota_empate, cuota_visitante,
               franja_temporal, capturado_en
        FROM mercado_cuotas_capturadas
        WHERE partido_id = :partido_id
          AND casa_apuestas = :casa_apuestas
          AND mercado = :mercado
        ORDER BY capturado_en DESC, id DESC
        LIMIT 1
    ");
    $stmt->execute([
        ':partido_id' => $partidoId,
        ':casa_apuestas' => $casaApuestas,
        ':mercado' => $mercado,
    ]);
    $ultima = $stmt->fetch();

    if ($ultima !== false) {
        $mismasCuotas =
            fmt_cuota($cuotaLocal)     === fmt_cuota($ultima['cuota_local'])
            && fmt_cuota($cuotaEmpate)    === fmt_cuota($ultima['cuota_empate'])
            && fmt_cuota($cuotaVisitante) === fmt_cuota($ultima['cuota_visitante']);

        $mismaFranja = (string)$franjaTemporal === (string)$ultima['franja_temporal'];

        if ($mismasCuotas && $mismaFranja) {
            api_json([
                'ok' => true,
                'accion' => 'sin_cambios',
                'id' => (int)$ultima['id'],
                'capturado_en_previo' => $ultima['capturado_en'],
            ]);
        }
    }

    // Salvaguarda: la misma captura exacta reenviada actualiza en lugar de
    // chocar contra el índice único.
    $stmt = $pdo->prepare("
        SELECT id
        FROM mercado_cuotas_capturadas
        WHERE partido_id = :partido_id
          AND casa_apuestas = :casa_apuestas
          AND mercado = :mercado
          AND capturado_en = :capturado_en
        LIMIT 1
    ");
    $stmt->execute([
        ':partido_id' => $partidoId,
        ':casa_apuestas' => $casaApuestas,
        ':mercado' => $mercado,
        ':capturado_en' => $capturadoEn,
    ]);
    $mismoInstante = $stmt->fetchColumn();

    if ($mismoInstante !== false) {
        $stmt = $pdo->prepare("
            UPDATE mercado_cuotas_capturadas SET
                franja_temporal = :franja_temporal,
                cuota_local = :cuota_local,
                cuota_empate = :cuota_empate,
                cuota_visitante = :cuota_visitante,
                prob_mercado_local = :prob_local,
                prob_mercado_empate = :prob_empate,
                prob_mercado_visitante = :prob_visitante,
                fuente = :fuente,
                origen_respuesta_id = :origen_respuesta_id
            WHERE id = :id
        ");
        $stmt->execute([
            ':franja_temporal' => $franjaTemporal,
            ':cuota_local' => $cuotaLocal,
            ':cuota_empate' => $cuotaEmpate,
            ':cuota_visitante' => $cuotaVisitante,
            ':prob_local' => $probLocal,
            ':prob_empate' => $probEmpate,
            ':prob_visitante' => $probVisitante,
            ':fuente' => $fuente,
            ':origen_respuesta_id' => $origenRespuestaId,
            ':id' => (int)$mismoInstante,
        ]);

        api_json([
            'ok' => true,
            'accion' => 'actualizado',
            'id' => (int)$mismoInstante,
            'overround' => $overround !== null ? round($overround, 6) : null,
        ]);
    }

    $stmt = $pdo->prepare("
        INSERT INTO mercado_cuotas_capturadas (
            partido_id, casa_apuestas, mercado, capturado_en, franja_temporal,
            cuota_local, cuota_empate, cuota_visitante,
            prob_mercado_local, prob_mercado_empate, prob_mercado_visitante,
            fuente, origen_respuesta_id
        ) VALUES (
            :partido_id, :casa_apuestas, :mercado, :capturado_en, :franja_temporal,
            :cuota_local, :cuota_empate, :cuota_visitante,
            :prob_local, :prob_empate, :prob_visitante,
            :fuente, :origen_respuesta_id
        )
    ");
    $stmt->execute([
        ':partido_id' => $partidoId,
        ':casa_apuestas' => $casaApuestas,
        ':mercado' => $mercado,
        ':capturado_en' => $capturadoEn,
        ':franja_temporal' => $franjaTemporal,
        ':cuota_local' => $cuotaLocal,
        ':cuota_empate' => $cuotaEmpate,
        ':cuota_visitante' => $cuotaVisitante,
        ':prob_local' => $probLocal,
        ':prob_empate' => $probEmpate,
        ':prob_visitante' => $probVisitante,
        ':fuente' => $fuente,
        ':origen_respuesta_id' => $origenRespuestaId,
    ]);

    api_json([
        'ok' => true,
        'accion' => 'creado',
        'id' => (int)$pdo->lastInsertId(),
        'overround' => $overround !== null ? round($overround, 6) : null,
        'probabilidades' => [
            'local' => round($probLocal, 6),
            'empate' => round($probEmpate, 6),
            'visitante' => round($probVisitante, 6),
        ],
    ], 201);
} catch (Throwable $e) {
    error_log('guardar_cuota.php: ' . $e->getMessage());
    api_fail('No se pudo guardar la cuota.', 500);
}
