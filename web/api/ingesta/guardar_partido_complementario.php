<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

$loteId = required_int($data, 'lote_id');
$fuente = str_required($data, 'fuente', 100);
$fuentesPermitidas = ['openfootball', 'supercopa-verificada', 'fixturedownload', 'wikipedia', 'fbref'];
if (!in_array($fuente, $fuentesPermitidas, true)) {
    api_fail('fuente no permitida.', 422);
}
$idPartidoFuente = str_required($data, 'id_partido_fuente', 180);
$temporada = str_required($data, 'temporada', 20);
$competicion = str_required($data, 'competicion', 100);
$ronda = isset($data['ronda']) && $data['ronda'] !== null
    ? trim((string)$data['ronda'])
    : null;

if ($ronda !== null && strlen($ronda) > 150) {
    api_fail('ronda demasiado larga.', 422);
}

$permitidas = [
    'Copa del Rey',
    'Supercopa de España',
    'UEFA Champions League',
    'UEFA Europa League',
    'UEFA Conference League',
];
if (!in_array($competicion, $permitidas, true)) {
    api_fail('competicion no permitida.', 422);
}

$fecha = normalize_datetime(
    isset($data['fecha_hora_inicio'])
        ? (string)$data['fecha_hora_inicio']
        : null,
    'fecha_hora_inicio'
);
if ($fecha === null) {
    api_fail('fecha_hora_inicio es obligatorio.', 422);
}

$horaConfirmada = !empty($data['hora_confirmada']) ? 1 : 0;
$esClasificatoria = !empty($data['es_clasificatoria']) ? 1 : 0;

$estado = strtoupper(str_required($data, 'estado', 20));
if (!in_array($estado, ['PROGRAMADO','FINALIZADO'], true)) {
    api_fail('estado inválido.', 422);
}

$localNombre = str_required($data, 'local_nombre', 150);
$visitanteNombre = str_required($data, 'visitante_nombre', 150);

$localIdFuente = isset($data['local_id_fuente']) && $data['local_id_fuente'] !== null
    ? trim((string)$data['local_id_fuente'])
    : null;
$visitanteIdFuente = isset($data['visitante_id_fuente']) && $data['visitante_id_fuente'] !== null
    ? trim((string)$data['visitante_id_fuente'])
    : null;

if ($localIdFuente !== null && strlen($localIdFuente) > 100) {
    api_fail('local_id_fuente demasiado largo.', 422);
}
if ($visitanteIdFuente !== null && strlen($visitanteIdFuente) > 100) {
    api_fail('visitante_id_fuente demasiado largo.', 422);
}

$localEquipoId = nullable_int($data, 'equipo_local_id');
$visitanteEquipoId = nullable_int($data, 'equipo_visitante_id');

if ($localEquipoId === null && $visitanteEquipoId === null) {
    api_fail(
        'El partido debe estar vinculado al menos a un equipo ya existente.',
        422
    );
}

$golesLocal = nullable_int($data, 'goles_local');
$golesVisitante = nullable_int($data, 'goles_visitante');

$huboProrroga = !empty($data['hubo_prorroga']) ? 1 : 0;
$huboPenaltis = !empty($data['hubo_penaltis']) ? 1 : 0;

$resultadoRaw = isset($data['resultado_raw']) && $data['resultado_raw'] !== null
    ? trim((string)$data['resultado_raw'])
    : null;
if ($resultadoRaw !== null && strlen($resultadoRaw) > 100) {
    api_fail('resultado_raw demasiado largo.', 422);
}

$url = isset($data['url']) && $data['url'] !== null
    ? trim((string)$data['url'])
    : null;
if ($url !== null && strlen($url) > 500) {
    api_fail('url demasiado larga.', 422);
}

$obtenidoEn = normalize_datetime(
    isset($data['obtenido_en'])
        ? (string)$data['obtenido_en']
        : null,
    'obtenido_en'
) ?? date('Y-m-d H:i:s');

$pdo = db();
$pdo->beginTransaction();

try {
    // Lote existente.
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

    // Temporada YA existente.
    $stmt = $pdo->prepare("
        SELECT temporada_id
        FROM nucleo_temporadas
        WHERE etiqueta = :temporada
        LIMIT 1
    ");
    $stmt->execute([':temporada' => $temporada]);
    $temporadaId = $stmt->fetchColumn();
    if (!$temporadaId) {
        api_fail('La temporada no existe en nucleo_temporadas.', 422);
    }
    $temporadaId = (int)$temporadaId;

    // Competición sembrada por la migración.
    $stmt = $pdo->prepare("
        SELECT competicion_id
        FROM nucleo_competiciones
        WHERE nombre = :nombre
          AND genero = 'MASCULINO'
        LIMIT 1
    ");
    $stmt->execute([':nombre' => $competicion]);
    $competicionId = $stmt->fetchColumn();
    if (!$competicionId) {
        api_fail(
            'La competición no existe. Ejecuta primero la migración SQL.',
            422
        );
    }
    $competicionId = (int)$competicionId;

    // Importante: SOLO comprobar equipos existentes. Nunca crear.
    foreach ([
        'local' => $localEquipoId,
        'visitante' => $visitanteEquipoId
    ] as $lado => $equipoId) {
        if ($equipoId === null) {
            continue;
        }
        $stmt = $pdo->prepare("
            SELECT equipo_id
            FROM nucleo_equipos
            WHERE equipo_id = :id
              AND genero = 'MASCULINO'
            LIMIT 1
        ");
        $stmt->execute([':id' => $equipoId]);
        if (!$stmt->fetch()) {
            api_fail(
                "equipo_{$lado}_id no existe o no es masculino.",
                422
            );
        }
    }

    $stmt = $pdo->prepare("
        SELECT
            partido_complementario_id,
            equipo_local_id,
            equipo_visitante_id,
            hora_confirmada,
            fecha_hora_inicio,
            estado,
            goles_local,
            goles_visitante
        FROM nucleo_partidos_complementarios
        WHERE fuente = :fuente
          AND id_partido_fuente = :id_fuente
        LIMIT 1
        FOR UPDATE
    ");
    $stmt->execute([':fuente' => $fuente, ':id_fuente' => $idPartidoFuente]);
    $existente = $stmt->fetch();

    if ($existente) {
        $partidoId = (int)$existente['partido_complementario_id'];

        $localExistente = $existente['equipo_local_id'] !== null
            ? (int)$existente['equipo_local_id']
            : null;
        $visitanteExistente = $existente['equipo_visitante_id'] !== null
            ? (int)$existente['equipo_visitante_id']
            : null;

        if (
            $localExistente !== null &&
            $localEquipoId !== null &&
            $localExistente !== $localEquipoId
        ) {
            api_fail(
                'Conflicto: el equipo local no coincide con el ya guardado.',
                409
            );
        }
        if (
            $visitanteExistente !== null &&
            $visitanteEquipoId !== null &&
            $visitanteExistente !== $visitanteEquipoId
        ) {
            api_fail(
                'Conflicto: el equipo visitante no coincide con el ya guardado.',
                409
            );
        }

        $localFinal = $localExistente ?? $localEquipoId;
        $visitanteFinal = $visitanteExistente ?? $visitanteEquipoId;

        $horaExistente = (int)$existente['hora_confirmada'];
        if ($horaExistente === 1 && $horaConfirmada === 0) {
            $fechaFinal = (string)$existente['fecha_hora_inicio'];
            $horaFinal = 1;
        } else {
            $fechaFinal = $fecha;
            $horaFinal = max($horaExistente, $horaConfirmada);
        }

        // No degradar un partido finalizado por una captura futura/antigua.
        $estadoFinal = (
            (string)$existente['estado'] === 'FINALIZADO'
            && $estado !== 'FINALIZADO'
        ) ? 'FINALIZADO' : $estado;

        $golesLocalFinal = $golesLocal;
        $golesVisitanteFinal = $golesVisitante;
        if ($golesLocalFinal === null && $existente['goles_local'] !== null) {
            $golesLocalFinal = (int)$existente['goles_local'];
        }
        if (
            $golesVisitanteFinal === null
            && $existente['goles_visitante'] !== null
        ) {
            $golesVisitanteFinal = (int)$existente['goles_visitante'];
        }

        $stmt = $pdo->prepare("
            UPDATE nucleo_partidos_complementarios
            SET lote_id = :lote,
                temporada_id = :temporada_id,
                competicion_id = :competicion_id,
                ronda = :ronda,
                es_clasificatoria = :clasif,
                fecha_hora_inicio = :fecha,
                zona_horaria = 'Europe/Madrid',
                hora_confirmada = :hora_confirmada,
                estado = :estado,
                equipo_local_id = :local_equipo,
                equipo_visitante_id = :visitante_equipo,
                local_nombre = :local_nombre,
                visitante_nombre = :visitante_nombre,
                local_id_fuente = :local_fuente,
                visitante_id_fuente = :visitante_fuente,
                goles_local = :goles_local,
                goles_visitante = :goles_visitante,
                hubo_prorroga = GREATEST(hubo_prorroga,:prorroga),
                hubo_penaltis = GREATEST(hubo_penaltis,:penaltis),
                resultado_raw = :resultado,
                url = :url,
                obtenido_en = :obtenido
            WHERE partido_complementario_id = :id
        ");
        $stmt->execute([
            ':lote' => $loteId,
            ':temporada_id' => $temporadaId,
            ':competicion_id' => $competicionId,
            ':ronda' => $ronda,
            ':clasif' => $esClasificatoria,
            ':fecha' => $fechaFinal,
            ':hora_confirmada' => $horaFinal,
            ':estado' => $estadoFinal,
            ':local_equipo' => $localFinal,
            ':visitante_equipo' => $visitanteFinal,
            ':local_nombre' => $localNombre,
            ':visitante_nombre' => $visitanteNombre,
            ':local_fuente' => $localIdFuente,
            ':visitante_fuente' => $visitanteIdFuente,
            ':goles_local' => $golesLocalFinal,
            ':goles_visitante' => $golesVisitanteFinal,
            ':prorroga' => $huboProrroga,
            ':penaltis' => $huboPenaltis,
            ':resultado' => $resultadoRaw,
            ':url' => $url,
            ':obtenido' => $obtenidoEn,
            ':id' => $partidoId,
        ]);
        $accion = 'actualizado';

    } else {
        $stmt = $pdo->prepare("
            INSERT INTO nucleo_partidos_complementarios
            (
                lote_id,fuente,id_partido_fuente,
                temporada_id,competicion_id,ronda,es_clasificatoria,
                fecha_hora_inicio,zona_horaria,hora_confirmada,estado,
                equipo_local_id,equipo_visitante_id,
                local_nombre,visitante_nombre,
                local_id_fuente,visitante_id_fuente,
                goles_local,goles_visitante,
                hubo_prorroga,hubo_penaltis,
                resultado_raw,url,obtenido_en
            )
            VALUES
            (
                :lote,:fuente,:id_fuente,
                :temporada_id,:competicion_id,:ronda,:clasif,
                :fecha,'Europe/Madrid',:hora_confirmada,:estado,
                :local_equipo,:visitante_equipo,
                :local_nombre,:visitante_nombre,
                :local_fuente,:visitante_fuente,
                :goles_local,:goles_visitante,
                :prorroga,:penaltis,
                :resultado,:url,:obtenido
            )
        ");
        $stmt->execute([
            ':lote' => $loteId,
            ':fuente' => $fuente,
            ':id_fuente' => $idPartidoFuente,
            ':temporada_id' => $temporadaId,
            ':competicion_id' => $competicionId,
            ':ronda' => $ronda,
            ':clasif' => $esClasificatoria,
            ':fecha' => $fecha,
            ':hora_confirmada' => $horaConfirmada,
            ':estado' => $estado,
            ':local_equipo' => $localEquipoId,
            ':visitante_equipo' => $visitanteEquipoId,
            ':local_nombre' => $localNombre,
            ':visitante_nombre' => $visitanteNombre,
            ':local_fuente' => $localIdFuente,
            ':visitante_fuente' => $visitanteIdFuente,
            ':goles_local' => $golesLocal,
            ':goles_visitante' => $golesVisitante,
            ':prorroga' => $huboProrroga,
            ':penaltis' => $huboPenaltis,
            ':resultado' => $resultadoRaw,
            ':url' => $url,
            ':obtenido' => $obtenidoEn,
        ]);
        $partidoId = (int)$pdo->lastInsertId();
        $accion = 'creado';
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'accion' => $accion,
        'partido_complementario_id' => $partidoId,
        'competicion_id' => $competicionId,
        'temporada_id' => $temporadaId,
        'equipo_local_id' => $localEquipoId,
        'equipo_visitante_id' => $visitanteEquipoId,
    ], $accion === 'creado' ? 201 : 200);

} catch (Throwable $e) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }

    if ($e->getMessage() === 'LOTE_NO_EXISTE') {
        api_fail('lote_id no existe.', 422);
    }

    error_log(
        'guardar_partido_complementario.php: ' . $e->getMessage()
    );
    api_fail('Error interno al guardar partido complementario.', 500);
}
