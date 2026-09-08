<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

$loteId = required_int($data, 'lote_id');
$fuente = str_required($data, 'fuente', 50);
$idPartidoFuente = str_required($data, 'id_partido_fuente', 180);
$temporadaEtiqueta = str_required($data, 'temporada_etiqueta', 20);
$jornada = required_int($data, 'jornada_numero');

$fechaInicio = normalize_datetime(
    isset($data['fecha_hora_inicio']) ? (string)$data['fecha_hora_inicio'] : null,
    'fecha_hora_inicio'
);
if ($fechaInicio === null) {
    api_fail('fecha_hora_inicio es obligatorio.', 422);
}

$estado = strtoupper(str_required($data, 'estado', 20));
$estadosPermitidos = [
    'PROGRAMADO', 'APLAZADO', 'EN_JUEGO', 'FINALIZADO',
    'SUSPENDIDO', 'CANCELADO', 'ADJUDICADO'
];
if (!in_array($estado, $estadosPermitidos, true)) {
    api_fail('estado inválido.', 422);
}

if (!isset($data['local']) || !is_array($data['local'])) {
    api_fail('local es obligatorio.', 422);
}
if (!isset($data['visitante']) || !is_array($data['visitante'])) {
    api_fail('visitante es obligatorio.', 422);
}

$localNombre = str_required($data['local'], 'nombre', 150);
$localFuenteId = str_required($data['local'], 'id_fuente', 100);
$visitanteNombre = str_required($data['visitante'], 'nombre', 150);
$visitanteFuenteId = str_required($data['visitante'], 'id_fuente', 100);

$golesLocal = nullable_int($data, 'goles_local');
$golesVisitante = nullable_int($data, 'goles_visitante');

$estadioNombre = null;
if (isset($data['estadio_nombre']) && $data['estadio_nombre'] !== null) {
    if (!is_string($data['estadio_nombre'])) {
        api_fail('estadio_nombre inválido.', 422);
    }
    $estadioNombre = trim($data['estadio_nombre']);
    if ($estadioNombre === '') {
        $estadioNombre = null;
    }
    if ($estadioNombre !== null && strlen($estadioNombre) > 150) {
        api_fail('estadio_nombre demasiado largo.', 422);
    }
}

$documentoUrl = str_required($data, 'documento_url', 500);
$contenidoRaw = str_required($data, 'contenido_raw', 3_000_000);
$obtenidoEn = normalize_datetime(
    isset($data['obtenido_en']) ? (string)$data['obtenido_en'] : null,
    'obtenido_en'
) ?? date('Y-m-d H:i:s');

$competicionNombre = isset($data['competicion_nombre'])
    ? trim((string)$data['competicion_nombre'])
    : 'Liga F';

$competicionPais = isset($data['competicion_pais'])
    ? trim((string)$data['competicion_pais'])
    : 'España';

$competicionGenero = isset($data['competicion_genero'])
    ? strtoupper(trim((string)$data['competicion_genero']))
    : 'FEMENINO';

$competicionNivel = isset($data['competicion_nivel'])
    ? strtoupper(trim((string)$data['competicion_nivel']))
    : 'PRIMERA_CATEGORIA';

$competicionOrganizador = isset($data['competicion_organizador'])
    ? trim((string)$data['competicion_organizador'])
    : ($competicionGenero === 'FEMENINO'
        ? 'Liga Profesional de Fútbol Femenino'
        : 'LaLiga');

$competicionAptaQuiniela = isset($data['competicion_apta_quiniela'])
    ? ((int)$data['competicion_apta_quiniela'] ? 1 : 0)
    : 1;

$generosPermitidos = ['MASCULINO', 'FEMENINO', 'MIXTO'];
if (!in_array($competicionGenero, $generosPermitidos, true)) {
    api_fail('competicion_genero inválido.', 422);
}

$nivelesPermitidos = [
    'PRIMERA_CATEGORIA', 'SEGUNDA_CATEGORIA', 'TERCERA_CATEGORIA',
    'COPA', 'SUPERCOPA', 'PLAYOFF', 'CLUB_INTERNACIONAL',
    'SELECCION_INTERNACIONAL', 'OTRO'
];
if (!in_array($competicionNivel, $nivelesPermitidos, true)) {
    api_fail('competicion_nivel inválido.', 422);
}

$temporadaFechaInicio = isset($data['temporada_fecha_inicio'])
    ? trim((string)$data['temporada_fecha_inicio'])
    : null;
$temporadaFechaFin = isset($data['temporada_fecha_fin'])
    ? trim((string)$data['temporada_fecha_fin'])
    : null;

// Compatibilidad: si los nuevos campos no llegan, se usan las fechas antiguas.
$competicionFechaInicio = isset($data['competicion_fecha_inicio'])
    ? trim((string)$data['competicion_fecha_inicio'])
    : $temporadaFechaInicio;
$competicionFechaFin = isset($data['competicion_fecha_fin'])
    ? trim((string)$data['competicion_fecha_fin'])
    : $temporadaFechaFin;

$numeroEquipos = isset($data['competicion_numero_equipos'])
    ? (int)$data['competicion_numero_equipos']
    : ($competicionGenero === 'FEMENINO' ? 16 : null);

$numeroJornadas = isset($data['competicion_numero_jornadas'])
    ? (int)$data['competicion_numero_jornadas']
    : ($competicionGenero === 'FEMENINO' ? 30 : null);

$formato = isset($data['competicion_formato'])
    ? trim((string)$data['competicion_formato'])
    : 'liga regular ida y vuelta';

$tipoContenido = isset($data['tipo_contenido_raw'])
    ? trim((string)$data['tipo_contenido_raw'])
    : 'partido';

if ($numeroEquipos !== null && ($numeroEquipos < 2 || $numeroEquipos > 100)) {
    api_fail('competicion_numero_equipos inválido.', 422);
}
if ($numeroJornadas !== null && ($numeroJornadas < 1 || $numeroJornadas > 100)) {
    api_fail('competicion_numero_jornadas inválido.', 422);
}
if ($tipoContenido === '' || strlen($tipoContenido) > 80) {
    api_fail('tipo_contenido_raw inválido.', 422);
}

function obtener_o_crear_competicion(
    PDO $pdo,
    string $nombre,
    string $pais,
    string $genero,
    string $nivel,
    string $organizador,
    int $aptaQuiniela
): int {
    $stmt = $pdo->prepare("
        SELECT competicion_id
        FROM nucleo_competiciones
        WHERE nombre = :nombre
          AND pais = :pais
          AND genero = :genero
        LIMIT 1
    ");
    $stmt->execute([
        ':nombre' => $nombre,
        ':pais' => $pais,
        ':genero' => $genero,
    ]);
    $row = $stmt->fetch();

    if ($row) {
        $id = (int)$row['competicion_id'];
        $stmt = $pdo->prepare("
            UPDATE nucleo_competiciones
            SET nivel_competicion = :nivel,
                organizador = :organizador,
                apta_quiniela = :apta
            WHERE competicion_id = :id
        ");
        $stmt->execute([
            ':nivel' => $nivel,
            ':organizador' => $organizador,
            ':apta' => $aptaQuiniela,
            ':id' => $id,
        ]);
        return $id;
    }

    $stmt = $pdo->prepare("
        INSERT INTO nucleo_competiciones
            (nombre, pais, nivel_competicion, genero, organizador, apta_quiniela)
        VALUES
            (:nombre, :pais, :nivel, :genero, :organizador, :apta)
    ");
    $stmt->execute([
        ':nombre' => $nombre,
        ':pais' => $pais,
        ':nivel' => $nivel,
        ':genero' => $genero,
        ':organizador' => $organizador,
        ':apta' => $aptaQuiniela,
    ]);
    return (int)$pdo->lastInsertId();
}

function obtener_o_crear_temporada(
    PDO $pdo,
    string $etiqueta,
    ?string $fechaInicio,
    ?string $fechaFin
): int {
    $stmt = $pdo->prepare("
        SELECT temporada_id
        FROM nucleo_temporadas
        WHERE etiqueta = :etiqueta
        LIMIT 1
    ");
    $stmt->execute([':etiqueta' => $etiqueta]);
    $row = $stmt->fetch();
    if ($row) {
        return (int)$row['temporada_id'];
    }

    if ($fechaInicio === null || $fechaInicio === '') {
        throw new RuntimeException('FALTA_FECHA_INICIO_TEMPORADA');
    }

    $stmt = $pdo->prepare("
        INSERT INTO nucleo_temporadas
            (etiqueta, fecha_inicio, fecha_fin)
        VALUES
            (:etiqueta, :inicio, :fin)
    ");
    $stmt->execute([
        ':etiqueta' => $etiqueta,
        ':inicio' => $fechaInicio,
        ':fin' => ($fechaFin === '' ? null : $fechaFin),
    ]);
    return (int)$pdo->lastInsertId();
}

function obtener_o_crear_tempcomp(
    PDO $pdo,
    int $competicionId,
    int $temporadaId,
    ?string $fechaInicio,
    ?string $fechaFin,
    ?int $numeroEquipos,
    ?int $numeroJornadas,
    string $formato
): int {
    $stmt = $pdo->prepare("
        SELECT temporada_competicion_id
        FROM nucleo_temporadas_competicion
        WHERE competicion_id = :competicion_id
          AND temporada_id = :temporada_id
        LIMIT 1
    ");
    $stmt->execute([
        ':competicion_id' => $competicionId,
        ':temporada_id' => $temporadaId,
    ]);
    $row = $stmt->fetch();

    if ($row) {
        $id = (int)$row['temporada_competicion_id'];

        // No borramos fechas ya conocidas con NULL.
        $stmt = $pdo->prepare("
            UPDATE nucleo_temporadas_competicion
            SET fecha_inicio = COALESCE(:inicio, fecha_inicio),
                fecha_fin = COALESCE(:fin, fecha_fin),
                numero_equipos = COALESCE(:equipos, numero_equipos),
                numero_jornadas = COALESCE(:jornadas, numero_jornadas),
                formato = COALESCE(NULLIF(:formato, ''), formato)
            WHERE temporada_competicion_id = :id
        ");
        $stmt->execute([
            ':inicio' => ($fechaInicio === '' ? null : $fechaInicio),
            ':fin' => ($fechaFin === '' ? null : $fechaFin),
            ':equipos' => $numeroEquipos,
            ':jornadas' => $numeroJornadas,
            ':formato' => $formato,
            ':id' => $id,
        ]);
        return $id;
    }

    $stmt = $pdo->prepare("
        INSERT INTO nucleo_temporadas_competicion
            (competicion_id, temporada_id, fecha_inicio, fecha_fin,
             numero_equipos, numero_jornadas, formato)
        VALUES
            (:competicion_id, :temporada_id, :inicio, :fin,
             :equipos, :jornadas, :formato)
    ");
    $stmt->execute([
        ':competicion_id' => $competicionId,
        ':temporada_id' => $temporadaId,
        ':inicio' => ($fechaInicio === '' ? null : $fechaInicio),
        ':fin' => ($fechaFin === '' ? null : $fechaFin),
        ':equipos' => $numeroEquipos,
        ':jornadas' => $numeroJornadas,
        ':formato' => $formato,
    ]);
    return (int)$pdo->lastInsertId();
}

function obtener_o_crear_fase(PDO $pdo, int $tempCompId): int
{
    $stmt = $pdo->prepare("
        SELECT fase_id
        FROM nucleo_fases_competicion
        WHERE temporada_competicion_id = :id
          AND nombre = 'Liga regular'
        LIMIT 1
    ");
    $stmt->execute([':id' => $tempCompId]);
    $row = $stmt->fetch();
    if ($row) {
        return (int)$row['fase_id'];
    }

    $stmt = $pdo->prepare("
        INSERT INTO nucleo_fases_competicion
            (temporada_competicion_id, nombre, es_eliminatoria, numero_ida_vuelta)
        VALUES
            (:id, 'Liga regular', 0, 2)
    ");
    $stmt->execute([':id' => $tempCompId]);
    return (int)$pdo->lastInsertId();
}

function obtener_o_crear_equipo(
    PDO $pdo,
    string $fuente,
    string $idFuente,
    string $nombre,
    string $genero,
    string $pais
): int {
    $stmt = $pdo->prepare("
        SELECT equipo_id
        FROM nucleo_ids_externos_equipo
        WHERE fuente = :fuente
          AND id_equipo_fuente = :id_fuente
        LIMIT 1
    ");
    $stmt->execute([
        ':fuente' => $fuente,
        ':id_fuente' => $idFuente,
    ]);
    $row = $stmt->fetch();
    if ($row) {
        return (int)$row['equipo_id'];
    }

    // Género en la búsqueda evita fusionar equipos masculino/femenino
    // con nombres iguales.
    $stmt = $pdo->prepare("
        SELECT equipo_id
        FROM nucleo_equipos
        WHERE nombre_canonico = :nombre
          AND genero = :genero
        LIMIT 1
    ");
    $stmt->execute([
        ':nombre' => $nombre,
        ':genero' => $genero,
    ]);
    $row = $stmt->fetch();

    if ($row) {
        $equipoId = (int)$row['equipo_id'];
    } else {
        $stmt = $pdo->prepare("
            INSERT INTO nucleo_equipos
                (nombre_canonico, nombre_corto, pais, genero, activo)
            VALUES
                (:nombre, :nombre_corto, :pais, :genero, 1)
        ");
        $stmt->execute([
            ':nombre' => $nombre,
            ':nombre_corto' => mb_substr($nombre, 0, 50),
            ':pais' => $pais,
            ':genero' => $genero,
        ]);
        $equipoId = (int)$pdo->lastInsertId();
    }

    $stmt = $pdo->prepare("
        INSERT INTO nucleo_ids_externos_equipo
            (equipo_id, fuente, id_equipo_fuente, confianza)
        VALUES
            (:equipo_id, :fuente, :id_fuente, 1.00)
    ");
    $stmt->execute([
        ':equipo_id' => $equipoId,
        ':fuente' => $fuente,
        ':id_fuente' => $idFuente,
    ]);

    return $equipoId;
}

function obtener_o_crear_estadio(PDO $pdo, ?string $nombre): ?int
{
    if ($nombre === null || $nombre === '') {
        return null;
    }

    $stmt = $pdo->prepare("
        SELECT estadio_id
        FROM nucleo_estadios
        WHERE nombre = :nombre
        LIMIT 1
    ");
    $stmt->execute([':nombre' => $nombre]);
    $row = $stmt->fetch();
    if ($row) {
        return (int)$row['estadio_id'];
    }

    $stmt = $pdo->prepare("
        INSERT INTO nucleo_estadios (nombre)
        VALUES (:nombre)
    ");
    $stmt->execute([':nombre' => $nombre]);
    return (int)$pdo->lastInsertId();
}

try {
    $pdo = db();
    $pdo->beginTransaction();

    $check = $pdo->prepare(
        "SELECT lote_id FROM bruto_lotes_ingesta WHERE lote_id = :id"
    );
    $check->execute([':id' => $loteId]);
    if (!$check->fetch()) {
        throw new RuntimeException('LOTE_NO_EXISTE');
    }

    // RAW siempre antes de normalizar.
    $hash = hash('sha256', $contenidoRaw);
    $stmt = $pdo->prepare("
        INSERT INTO bruto_documentos
            (lote_id, fuente, url, tipo_contenido, obtenido_en,
             contenido, hash_contenido)
        VALUES
            (:lote_id, :fuente, :url, :tipo,
             :obtenido_en, :contenido, :hash)
    ");
    $stmt->execute([
        ':lote_id' => $loteId,
        ':fuente' => $fuente,
        ':url' => $documentoUrl,
        ':tipo' => $tipoContenido,
        ':obtenido_en' => $obtenidoEn,
        ':contenido' => $contenidoRaw,
        ':hash' => $hash,
    ]);
    $documentoId = (int)$pdo->lastInsertId();

    $competicionId = obtener_o_crear_competicion(
        $pdo,
        $competicionNombre,
        $competicionPais,
        $competicionGenero,
        $competicionNivel,
        $competicionOrganizador,
        $competicionAptaQuiniela
    );

    $temporadaId = obtener_o_crear_temporada(
        $pdo,
        $temporadaEtiqueta,
        $temporadaFechaInicio,
        $temporadaFechaFin
    );

    $tempCompId = obtener_o_crear_tempcomp(
        $pdo,
        $competicionId,
        $temporadaId,
        $competicionFechaInicio,
        $competicionFechaFin,
        $numeroEquipos,
        $numeroJornadas,
        $formato
    );

    $faseId = obtener_o_crear_fase($pdo, $tempCompId);

    $localId = obtener_o_crear_equipo(
        $pdo,
        $fuente,
        $localFuenteId,
        $localNombre,
        $competicionGenero,
        $competicionPais
    );
    $visitanteId = obtener_o_crear_equipo(
        $pdo,
        $fuente,
        $visitanteFuenteId,
        $visitanteNombre,
        $competicionGenero,
        $competicionPais
    );

    if ($localId === $visitanteId) {
        throw new RuntimeException('EQUIPOS_IGUALES');
    }

    $estadioId = obtener_o_crear_estadio($pdo, $estadioNombre);

    $stmt = $pdo->prepare("
        SELECT partido_id
        FROM nucleo_ids_externos_partido
        WHERE fuente = :fuente
          AND id_partido_fuente = :id_fuente
        LIMIT 1
    ");
    $stmt->execute([
        ':fuente' => $fuente,
        ':id_fuente' => $idPartidoFuente,
    ]);
    $row = $stmt->fetch();

    if ($row) {
        $partidoId = (int)$row['partido_id'];

        $stmt = $pdo->prepare("
            UPDATE nucleo_partidos
            SET temporada_competicion_id = :tempcomp,
                fase_id = :fase,
                jornada_numero = :jornada,
                equipo_local_id = :local,
                equipo_visitante_id = :visitante,
                estadio_id = :estadio,
                fecha_hora_inicio = :fecha,
                estado = :estado,
                goles_local = :goles_local,
                goles_visitante = :goles_visitante
            WHERE partido_id = :partido_id
        ");
        $stmt->execute([
            ':tempcomp' => $tempCompId,
            ':fase' => $faseId,
            ':jornada' => $jornada,
            ':local' => $localId,
            ':visitante' => $visitanteId,
            ':estadio' => $estadioId,
            ':fecha' => $fechaInicio,
            ':estado' => $estado,
            ':goles_local' => $golesLocal,
            ':goles_visitante' => $golesVisitante,
            ':partido_id' => $partidoId,
        ]);
        $accion = 'actualizado';
    } else {
        $stmt = $pdo->prepare("
            INSERT INTO nucleo_partidos
                (temporada_competicion_id, fase_id, jornada_numero,
                 equipo_local_id, equipo_visitante_id, estadio_id,
                 fecha_hora_inicio, estado, goles_local, goles_visitante)
            VALUES
                (:tempcomp, :fase, :jornada,
                 :local, :visitante, :estadio,
                 :fecha, :estado, :goles_local, :goles_visitante)
        ");
        $stmt->execute([
            ':tempcomp' => $tempCompId,
            ':fase' => $faseId,
            ':jornada' => $jornada,
            ':local' => $localId,
            ':visitante' => $visitanteId,
            ':estadio' => $estadioId,
            ':fecha' => $fechaInicio,
            ':estado' => $estado,
            ':goles_local' => $golesLocal,
            ':goles_visitante' => $golesVisitante,
        ]);
        $partidoId = (int)$pdo->lastInsertId();

        $stmt = $pdo->prepare("
            INSERT INTO nucleo_ids_externos_partido
                (partido_id, fuente, id_partido_fuente)
            VALUES
                (:partido_id, :fuente, :id_fuente)
        ");
        $stmt->execute([
            ':partido_id' => $partidoId,
            ':fuente' => $fuente,
            ':id_fuente' => $idPartidoFuente,
        ]);
        $accion = 'creado';
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'partido_id' => $partidoId,
        'documento_raw_id' => $documentoId,
        'accion' => $accion,
        'competicion_id' => $competicionId,
        'temporada_competicion_id' => $tempCompId,
        'equipo_local_id' => $localId,
        'equipo_visitante_id' => $visitanteId,
        'estadio_id' => $estadioId,
    ], $accion === 'creado' ? 201 : 200);

} catch (Throwable $e) {
    if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) {
        $pdo->rollBack();
    }

    if ($e->getMessage() === 'LOTE_NO_EXISTE') {
        api_fail('lote_id no existe.', 422);
    }
    if ($e->getMessage() === 'FALTA_FECHA_INICIO_TEMPORADA') {
        api_fail('Falta fecha_inicio para crear una temporada nueva.', 422);
    }
    if ($e->getMessage() === 'EQUIPOS_IGUALES') {
        api_fail('Local y visitante se resolvieron al mismo equipo.', 422);
    }

    error_log('guardar_partido.php: ' . $e->getMessage());
    api_fail('No se pudo guardar el partido.', 500);
}
