<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

/**
 * Importa una jornada completa de La Quiniela.
 *
 * Escribe en cinco tablas dentro de una sola transacción:
 *   quiniela_jornadas          la jornada
 *   quiniela_partidos_jornada  las casillas del boleto
 *   quiniela_probabilidades    probabilidad de mercado y proporción apostada
 *   quiniela_pleno15           el Pleno al 15 (goles por equipo)
 *   quiniela_escrutinios       recaudación, acertantes, premios
 *
 * Payload:
 *   numero_jornada      int      obligatorio
 *   etiqueta_temporada  string   obligatorio (formato '2026-27')
 *   fecha_sorteo        date     opcional
 *   fuente              string   opcional, por defecto 'quinielandia'
 *   fuente_fichero      string   opcional
 *   tipo_lae            string   opcional, por defecto 'CIERRE'
 *   tipo_mercado        string   opcional, por defecto 'CIERRE'
 *   casillas            array    1..15 elementos:
 *       posicion, equipo_local_impreso, equipo_visitante_impreso
 *       signo_oficial        opcional, 1/X/2 (casillas 1..14)
 *       signo_pleno_oficial  opcional, solo casilla 15: dos caracteres de
 *                            0/1/2/M ('11', 'M2', '0M'); se guarda '1-1' 
 *       partido_id         opcional
 *       prob_mercado       {p1, px, p2}  opcional, fracciones 0..1
 *       prob_lae           {p1, px, p2}  opcional
 *   pleno15             objeto   opcional:
 *       lae_local, lae_visitante, modelo_local, modelo_visitante
 *       cada uno {p0, p1, p2, pm}, fracciones 0..1
 *   escrutinio          objeto   opcional
 *
 * Sobre `tipo_lae`: los porcentajes cambian mientras la gente apuesta, así
 * que una captura del jueves no es la del cierre. Guardarlas todas como
 * 'CIERRE' haría creer más adelante que un dato de T-24 era el definitivo,
 * que es exactamente el data-leakage que las reglas del proyecto evitan con
 * el clima. Por eso el tipo viaja en el payload y no está fijado aquí.
 */

$numeroJornada = required_int($data, 'numero_jornada');
if ($numeroJornada < 1 || $numeroJornada > 99) {
    api_fail('numero_jornada fuera de rango (1..99).', 422);
}

$etiquetaTemporada = str_required($data, 'etiqueta_temporada', 20);

$fechaSorteo = normalize_datetime(
    isset($data['fecha_sorteo']) ? (string)$data['fecha_sorteo'] : null,
    'fecha_sorteo'
);
if ($fechaSorteo !== null) {
    $fechaSorteo = substr($fechaSorteo, 0, 10);
}

$fuente = isset($data['fuente']) && $data['fuente'] !== null && $data['fuente'] !== ''
    ? str_required($data, 'fuente', 50)
    : 'quinielandia';

$fuenteFichero = null;
if (isset($data['fuente_fichero']) && $data['fuente_fichero'] !== null && $data['fuente_fichero'] !== '') {
    $fuenteFichero = str_required($data, 'fuente_fichero', 150);
}

$TIPOS = ['CIERRE', 'APERTURA', 'T-2', 'T-24', 'T-72', 'T-72+', 'ESTIMADO', 'DESCONOCIDA'];

function tipo_valido(array $data, string $clave, array $permitidos): string
{
    if (!isset($data[$clave]) || $data[$clave] === null || $data[$clave] === '') {
        return 'CIERRE';
    }
    $v = strtoupper(trim((string)$data[$clave]));
    if (!in_array($v, $permitidos, true)) {
        api_fail(
            "$clave debe ser uno de: " . implode(', ', $permitidos) . ".",
            422
        );
    }
    return $v;
}

$tipoLae = tipo_valido($data, 'tipo_lae', $TIPOS);
$tipoMercado = tipo_valido($data, 'tipo_mercado', $TIPOS);

if (!isset($data['casillas']) || !is_array($data['casillas']) || $data['casillas'] === []) {
    api_fail('casillas debe ser un array con al menos un elemento.', 422);
}
if (count($data['casillas']) > 15) {
    api_fail('casillas no puede tener más de 15 elementos.', 422);
}

/** Valida una terna 1/X/2 en fracciones 0..1 que sume aproximadamente 1. */
function terna_probabilidades($valor, string $etiqueta): ?array
{
    if ($valor === null || !is_array($valor)) {
        return null;
    }
    $salida = [];
    foreach (['p1', 'px', 'p2'] as $clave) {
        if (!array_key_exists($clave, $valor) || $valor[$clave] === null) {
            return null;
        }
        if (!is_numeric($valor[$clave])) {
            api_fail("$etiqueta.$clave debe ser numérico.", 422);
        }
        $p = (float)$valor[$clave];
        if ($p < 0.0 || $p > 1.0) {
            api_fail("$etiqueta.$clave debe estar entre 0 y 1 (recibido: $p).", 422);
        }
        $salida[$clave] = $p;
    }
    $suma = $salida['p1'] + $salida['px'] + $salida['p2'];
    if ($suma < 0.95 || $suma > 1.05) {
        api_fail("$etiqueta debe sumar aproximadamente 1 (suma: " . round($suma, 4) . ").", 422);
    }
    return $salida;
}

/** Igual, pero para los cuatro valores de goles del Pleno (0/1/2/M). */
function cuarteto_goles($valor, string $etiqueta): ?array
{
    if ($valor === null || !is_array($valor)) {
        return null;
    }
    $salida = [];
    foreach (['p0', 'p1', 'p2', 'pm'] as $clave) {
        if (!array_key_exists($clave, $valor) || $valor[$clave] === null) {
            return null;
        }
        if (!is_numeric($valor[$clave])) {
            api_fail("$etiqueta.$clave debe ser numérico.", 422);
        }
        $p = (float)$valor[$clave];
        if ($p < 0.0 || $p > 1.0) {
            api_fail("$etiqueta.$clave debe estar entre 0 y 1 (recibido: $p).", 422);
        }
        $salida[$clave] = $p;
    }
    $suma = array_sum($salida);
    if ($suma < 0.95 || $suma > 1.05) {
        api_fail("$etiqueta debe sumar aproximadamente 1 (suma: " . round($suma, 4) . ").", 422);
    }
    return $salida;
}

// --- Casillas ------------------------------------------------------------

$signosValidos = ['1', 'X', '2'];
$casillas = [];
$vistas = [];

foreach ($data['casillas'] as $i => $c) {
    if (!is_array($c)) {
        api_fail("casillas[$i] debe ser un objeto.", 422);
    }
    $posicion = required_int($c, 'posicion');
    if ($posicion < 1 || $posicion > 15) {
        api_fail("casillas[$i].posicion fuera de rango (1..15).", 422);
    }
    if (isset($vistas[$posicion])) {
        api_fail("posicion $posicion duplicada en el payload.", 422);
    }
    $vistas[$posicion] = true;

    $signo = null;
    if (isset($c['signo_oficial']) && $c['signo_oficial'] !== null && $c['signo_oficial'] !== '') {
        $signo = strtoupper(trim((string)$c['signo_oficial']));
        if (!in_array($signo, $signosValidos, true)) {
            api_fail("casillas[$i].signo_oficial debe ser 1, X o 2.", 422);
        }
    }

    // El resultado del Pleno no es 1/X/2 sino los goles de cada equipo.
    // SELAE lo manda de varias formas ('11', 'M2', '0M', '1-1'), así que se
    // normaliza a dos caracteres de {0,1,2,M}: local y visitante.
    $signoPleno = null;
    if ($posicion === 15
        && isset($c['signo_pleno_oficial'])
        && $c['signo_pleno_oficial'] !== null
        && $c['signo_pleno_oficial'] !== ''
    ) {
        $bruto = strtoupper(str_replace(['-', ' '], '', (string)$c['signo_pleno_oficial']));
        if (!preg_match('/^[012M]{2}$/', $bruto)) {
            api_fail(
                "casillas[$i].signo_pleno_oficial debe ser dos caracteres de "
                . "0, 1, 2 o M (recibido: '$bruto').",
                422
            );
        }
        $signoPleno = $bruto[0] . '-' . $bruto[1];
    }

    $casillas[] = [
        'posicion' => $posicion,
        'local' => str_required($c, 'equipo_local_impreso', 100),
        'visitante' => str_required($c, 'equipo_visitante_impreso', 100),
        'signo' => $signo,
        'signo_pleno' => $signoPleno,
        'partido_id' => nullable_int($c, 'partido_id'),
        'mercado' => terna_probabilidades($c['prob_mercado'] ?? null, "casillas[$i].prob_mercado"),
        'lae' => terna_probabilidades($c['prob_lae'] ?? null, "casillas[$i].prob_lae"),
    ];
}

// --- Pleno al 15 ---------------------------------------------------------

$pleno = null;
if (isset($data['pleno15']) && is_array($data['pleno15'])) {
    $p15 = $data['pleno15'];
    $pleno = [
        'LAE_CIERRE' => [
            'LOCAL' => cuarteto_goles($p15['lae_local'] ?? null, 'pleno15.lae_local'),
            'VISITANTE' => cuarteto_goles($p15['lae_visitante'] ?? null, 'pleno15.lae_visitante'),
        ],
        'MODELO_PROPIO' => [
            'LOCAL' => cuarteto_goles($p15['modelo_local'] ?? null, 'pleno15.modelo_local'),
            'VISITANTE' => cuarteto_goles($p15['modelo_visitante'] ?? null, 'pleno15.modelo_visitante'),
        ],
    ];
}

// --- Escrutinio ----------------------------------------------------------

$escrutinio = null;
if (isset($data['escrutinio']) && is_array($data['escrutinio'])) {
    $e = $data['escrutinio'];
    $escrutinio = [
        'precio_apuesta' => nullable_float($e, 'precio_apuesta'),
        'pct_categoria_14' => nullable_float($e, 'pct_categoria_14'),
        'recaudacion' => nullable_float($e, 'recaudacion'),
        'apuestas_validadas' => nullable_int($e, 'apuestas_validadas'),
        'acertantes_14' => nullable_int($e, 'acertantes_14'),
        'premio_14' => nullable_float($e, 'premio_14'),
        'premio_14_normalizado' => nullable_float($e, 'premio_14_normalizado'),
        'informacion_mercado' => nullable_float($e, 'informacion_mercado'),
        'informacion_lae' => nullable_float($e, 'informacion_lae'),
        'coeficiente' => nullable_float($e, 'coeficiente'),
        'coeficiente_absoluto' => nullable_float($e, 'coeficiente_absoluto'),
        'coeficiente_mayor_1' => nullable_float($e, 'coeficiente_mayor_1'),
        'coeficiente_abs_mayor_1' => nullable_float($e, 'coeficiente_abs_mayor_1'),
        'em_14' => nullable_float($e, 'em_14'),
        'em_14_absoluto' => nullable_float($e, 'em_14_absoluto'),
    ];
}

// --- Validación de partido_id -------------------------------------------

$idsPartido = array_values(array_filter(
    array_map(static fn(array $c) => $c['partido_id'], $casillas),
    static fn($v) => $v !== null
));

if ($idsPartido !== []) {
    $ph = implode(',', array_fill(0, count($idsPartido), '?'));
    $stmt = db()->prepare("SELECT partido_id FROM nucleo_partidos WHERE partido_id IN ($ph)");
    $stmt->execute($idsPartido);
    $encontrados = array_map('intval', $stmt->fetchAll(PDO::FETCH_COLUMN));
    $faltan = array_diff($idsPartido, $encontrados);
    if ($faltan !== []) {
        api_fail('partido_id inexistentes en nucleo_partidos: ' . implode(', ', $faltan), 422);
    }
}

// --- Guardado transaccional ----------------------------------------------

try {
    $pdo = db();
    $pdo->beginTransaction();

    $stmt = $pdo->prepare("
        SELECT jornada_id FROM quiniela_jornadas
        WHERE numero_jornada = :n AND etiqueta_temporada = :t
        LIMIT 1
    ");
    $stmt->execute([':n' => $numeroJornada, ':t' => $etiquetaTemporada]);
    $jornadaId = $stmt->fetchColumn();

    if ($jornadaId === false) {
        $stmt = $pdo->prepare("
            INSERT INTO quiniela_jornadas
                (numero_jornada, etiqueta_temporada, fecha_sorteo, fuente)
            VALUES (:n, :t, :f, :fu)
        ");
        $stmt->execute([
            ':n' => $numeroJornada, ':t' => $etiquetaTemporada,
            ':f' => $fechaSorteo, ':fu' => $fuente,
        ]);
        $jornadaId = (int)$pdo->lastInsertId();
        $accion = 'creada';
    } else {
        $jornadaId = (int)$jornadaId;
        $stmt = $pdo->prepare("
            UPDATE quiniela_jornadas
            SET fecha_sorteo = COALESCE(:f, fecha_sorteo), fuente = :fu
            WHERE jornada_id = :id
        ");
        $stmt->execute([':f' => $fechaSorteo, ':fu' => $fuente, ':id' => $jornadaId]);
        $accion = 'actualizada';
    }

    $insCasilla = $pdo->prepare("
        INSERT INTO quiniela_partidos_jornada
            (jornada_id, posicion, partido_id,
             equipo_local_impreso, equipo_visitante_impreso,
             signo_oficial, signo_pleno_oficial)
        VALUES (:j, :p, :pid, :loc, :vis, :s, :sp)
        ON DUPLICATE KEY UPDATE
            partido_id = COALESCE(VALUES(partido_id), partido_id),
            equipo_local_impreso = VALUES(equipo_local_impreso),
            equipo_visitante_impreso = VALUES(equipo_visitante_impreso),
            signo_oficial = COALESCE(VALUES(signo_oficial), signo_oficial),
            signo_pleno_oficial = COALESCE(VALUES(signo_pleno_oficial), signo_pleno_oficial)
    ");

    $insProb = $pdo->prepare("
        INSERT INTO quiniela_probabilidades
            (jornada_id, posicion, fuente, tipo, prob_1, prob_x, prob_2,
             es_estimado, calidad, fuente_fichero)
        VALUES (:j, :p, :fu, :ti, :p1, :px, :p2, :est, :cal, :ff)
        ON DUPLICATE KEY UPDATE
            prob_1 = VALUES(prob_1), prob_x = VALUES(prob_x),
            prob_2 = VALUES(prob_2), fuente_fichero = VALUES(fuente_fichero)
    ");

    $nProb = 0;
    foreach ($casillas as $c) {
        $insCasilla->execute([
            ':j' => $jornadaId, ':p' => $c['posicion'], ':pid' => $c['partido_id'],
            ':loc' => $c['local'], ':vis' => $c['visitante'], ':s' => $c['signo'],
            ':sp' => $c['signo_pleno'],
        ]);

        if ($c['mercado'] !== null) {
            $insProb->execute([
                ':j' => $jornadaId, ':p' => $c['posicion'],
                ':fu' => 'BETFAIR_QUINIELANDIA', ':ti' => $tipoMercado,
                ':p1' => $c['mercado']['p1'], ':px' => $c['mercado']['px'],
                ':p2' => $c['mercado']['p2'],
                ':est' => $tipoMercado === 'ESTIMADO' ? 1 : 0,
                ':cal' => 'HISTORICO_IMPORTADO', ':ff' => $fuenteFichero,
            ]);
            $nProb++;
        }

        if ($c['lae'] !== null) {
            // La fuente distingue si el dato es definitivo o una foto previa:
            // el porcentaje al cierre y el de T-24 no son el mismo dato.
            $fuenteLae = $tipoLae === 'CIERRE' ? 'LAE_CIERRE' : 'LAE_ESTIMADO';
            $insProb->execute([
                ':j' => $jornadaId, ':p' => $c['posicion'],
                ':fu' => $fuenteLae, ':ti' => $tipoLae,
                ':p1' => $c['lae']['p1'], ':px' => $c['lae']['px'],
                ':p2' => $c['lae']['p2'],
                ':est' => $tipoLae === 'CIERRE' ? 0 : 1,
                ':cal' => 'HISTORICO_IMPORTADO', ':ff' => $fuenteFichero,
            ]);
            $nProb++;
        }
    }

    // --- Pleno al 15 -----------------------------------------------------

    $nPleno = 0;
    if ($pleno !== null) {
        $insPleno = $pdo->prepare("
            INSERT INTO quiniela_pleno15
                (jornada_id, fuente, tipo, lado, prob_0, prob_1, prob_2, prob_m,
                 es_estimado, calidad, fuente_fichero)
            VALUES (:j, :fu, :ti, :lado, :p0, :p1, :p2, :pm, :est, :cal, :ff)
            ON DUPLICATE KEY UPDATE
                prob_0 = VALUES(prob_0), prob_1 = VALUES(prob_1),
                prob_2 = VALUES(prob_2), prob_m = VALUES(prob_m),
                fuente_fichero = VALUES(fuente_fichero)
        ");

        foreach ($pleno as $fuentePleno => $lados) {
            foreach ($lados as $lado => $valores) {
                if ($valores === null) {
                    continue;
                }
                $tipoPleno = $fuentePleno === 'MODELO_PROPIO' ? 'ESTIMADO' : $tipoLae;
                if ($fuentePleno === 'LAE_CIERRE' && $tipoLae !== 'CIERRE') {
                    $fuentePleno = 'LAE_ESTIMADO';
                }
                $insPleno->execute([
                    ':j' => $jornadaId, ':fu' => $fuentePleno, ':ti' => $tipoPleno,
                    ':lado' => $lado,
                    ':p0' => $valores['p0'], ':p1' => $valores['p1'],
                    ':p2' => $valores['p2'], ':pm' => $valores['pm'],
                    ':est' => $tipoPleno === 'CIERRE' ? 0 : 1,
                    ':cal' => 'IMPORTADO', ':ff' => $fuenteFichero,
                ]);
                $nPleno++;
            }
        }
    }

    // --- Escrutinio ------------------------------------------------------

    $conEscrutinio = false;
    if ($escrutinio !== null) {
        $stmt = $pdo->prepare("
            INSERT INTO quiniela_escrutinios (
                jornada_id, precio_apuesta, pct_categoria_14, recaudacion,
                apuestas_validadas, acertantes_14, premio_14,
                premio_14_normalizado, informacion_mercado, informacion_lae,
                coeficiente, coeficiente_absoluto, coeficiente_mayor_1,
                coeficiente_abs_mayor_1, em_14, em_14_absoluto,
                fuente, fuente_fichero
            ) VALUES (
                :j, :precio, :pct, :rec, :ap, :ac, :pr14, :pr14n,
                :infm, :infl, :coef, :coefabs, :coefm1, :coefabsm1,
                :em, :emabs, :fu, :ff
            )
            ON DUPLICATE KEY UPDATE
                precio_apuesta = VALUES(precio_apuesta),
                pct_categoria_14 = VALUES(pct_categoria_14),
                recaudacion = VALUES(recaudacion),
                apuestas_validadas = VALUES(apuestas_validadas),
                acertantes_14 = VALUES(acertantes_14),
                premio_14 = VALUES(premio_14),
                premio_14_normalizado = VALUES(premio_14_normalizado),
                informacion_mercado = VALUES(informacion_mercado),
                informacion_lae = VALUES(informacion_lae),
                coeficiente = VALUES(coeficiente),
                coeficiente_absoluto = VALUES(coeficiente_absoluto),
                coeficiente_mayor_1 = VALUES(coeficiente_mayor_1),
                coeficiente_abs_mayor_1 = VALUES(coeficiente_abs_mayor_1),
                em_14 = VALUES(em_14), em_14_absoluto = VALUES(em_14_absoluto),
                fuente_fichero = VALUES(fuente_fichero)
        ");
        $stmt->execute([
            ':j' => $jornadaId,
            ':precio' => $escrutinio['precio_apuesta'],
            ':pct' => $escrutinio['pct_categoria_14'],
            ':rec' => $escrutinio['recaudacion'],
            ':ap' => $escrutinio['apuestas_validadas'],
            ':ac' => $escrutinio['acertantes_14'],
            ':pr14' => $escrutinio['premio_14'],
            ':pr14n' => $escrutinio['premio_14_normalizado'],
            ':infm' => $escrutinio['informacion_mercado'],
            ':infl' => $escrutinio['informacion_lae'],
            ':coef' => $escrutinio['coeficiente'],
            ':coefabs' => $escrutinio['coeficiente_absoluto'],
            ':coefm1' => $escrutinio['coeficiente_mayor_1'],
            ':coefabsm1' => $escrutinio['coeficiente_abs_mayor_1'],
            ':em' => $escrutinio['em_14'],
            ':emabs' => $escrutinio['em_14_absoluto'],
            ':fu' => $fuente, ':ff' => $fuenteFichero,
        ]);
        $conEscrutinio = true;
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'accion' => $accion,
        'jornada_id' => $jornadaId,
        'casillas' => count($casillas),
        'probabilidades' => $nProb,
        'pleno15' => $nPleno,
        'escrutinio' => $conEscrutinio,
        'tipo_lae' => $tipoLae,
    ], $accion === 'creada' ? 201 : 200);
} catch (Throwable $e) {
    if (isset($pdo) && $pdo->inTransaction()) {
        $pdo->rollBack();
    }
    error_log('importar_jornada_historica.php: ' . $e->getMessage());
    api_fail('No se pudo importar la jornada.', 500);
}
