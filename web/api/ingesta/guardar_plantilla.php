<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('POST');
require_auth();

$data = json_body();

$loteId = required_int($data, 'lote_id');
$equipoId = required_int($data, 'equipo_id');
$fuente = str_required($data, 'fuente', 50);

$fechaPlantilla = isset($data['fecha_plantilla']) && is_string($data['fecha_plantilla'])
    ? trim($data['fecha_plantilla'])
    : date('Y-m-d');

if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $fechaPlantilla)) {
    api_fail('fecha_plantilla debe tener formato YYYY-MM-DD.', 422);
}

$jugadores = $data['jugadores'] ?? null;
if (!is_array($jugadores)) {
    api_fail('jugadores debe ser un array.', 422);
}

if (count($jugadores) < 15) {
    api_fail(
        'La plantilla recibida tiene menos de 15 jugadores; '
        . 'se rechaza para evitar cerrar pertenencias por un parseo incompleto.',
        422
    );
}

function normalizar_nombre_jugador(string $valor): string {
    $valor = trim($valor);
    $ascii = @iconv('UTF-8', 'ASCII//TRANSLIT//IGNORE', $valor);
    if ($ascii === false) {
        $ascii = $valor;
    }
    $ascii = strtolower($ascii);
    return preg_replace('/[^a-z0-9]+/', '', $ascii) ?? '';
}

function nullable_string_local(array $data, string $campo, int $max): ?string {
    if (!array_key_exists($campo, $data) || $data[$campo] === null) {
        return null;
    }
    if (!is_string($data[$campo])) {
        api_fail("$campo debe ser texto o null.", 422);
    }
    $v = trim($data[$campo]);
    if ($v === '') {
        return null;
    }
    if (strlen($v) > $max) {
        api_fail("$campo supera $max caracteres.", 422);
    }
    return $v;
}

function nullable_date_local(array $data, string $campo): ?string {
    $v = nullable_string_local($data, $campo, 10);
    if ($v === null) {
        return null;
    }
    if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $v)) {
        api_fail("$campo debe tener formato YYYY-MM-DD.", 422);
    }
    return $v;
}

function nullable_int_local(array $data, string $campo): ?int {
    if (!array_key_exists($campo, $data) || $data[$campo] === null || $data[$campo] === '') {
        return null;
    }
    if (!is_numeric($data[$campo])) {
        api_fail("$campo debe ser numérico o null.", 422);
    }
    return (int)$data[$campo];
}

try {
    $pdo = db();
    $pdo->beginTransaction();

    $stmt = $pdo->prepare("
        SELECT equipo_id
        FROM nucleo_equipos
        WHERE equipo_id = :equipo_id
        LIMIT 1
        FOR UPDATE
    ");
    $stmt->execute([':equipo_id' => $equipoId]);
    if (!$stmt->fetch()) {
        throw new RuntimeException('EQUIPO_NO_EXISTE');
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

    // Candidatos activos actuales del equipo: nos permiten enganchar
    // los registros seed existentes aunque aún no tengan ID externo.
    $stmt = $pdo->prepare("
        SELECT
            j.jugador_id,
            j.nombre_completo,
            p.dorsal
        FROM nucleo_pertenencias_jugador_equipo p
        JOIN nucleo_jugadores j
            ON j.jugador_id = p.jugador_id
        WHERE p.equipo_id = :equipo_id
          AND p.vigente_hasta IS NULL
    ");
    $stmt->execute([':equipo_id' => $equipoId]);
    $candidatos = $stmt->fetchAll();

    $mapaNombre = [];
    foreach ($candidatos as $cand) {
        $clave = normalizar_nombre_jugador((string)$cand['nombre_completo']);
        if ($clave === '') {
            continue;
        }
        if (!isset($mapaNombre[$clave])) {
            $mapaNombre[$clave] = [];
        }
        $mapaNombre[$clave][] = [
            'jugador_id' => (int)$cand['jugador_id'],
            'dorsal' => $cand['dorsal'] !== null ? (int)$cand['dorsal'] : null,
        ];
    }

    $idsProcesados = [];
    $creados = 0;
    $actualizados = 0;
    $vinculosNuevos = 0;

    foreach ($jugadores as $idx => $jugador) {
        if (!is_array($jugador)) {
            api_fail("jugadores[$idx] debe ser un objeto.", 422);
        }

        $idFuente = nullable_string_local($jugador, 'id_fuente', 100);
        $nombre = nullable_string_local($jugador, 'nombre_completo', 150);

        if ($idFuente === null || $nombre === null) {
            api_fail(
                "jugadores[$idx] requiere id_fuente y nombre_completo.",
                422
            );
        }

        $fechaNacimiento = nullable_date_local($jugador, 'fecha_nacimiento');
        $nacionalidad = nullable_string_local($jugador, 'nacionalidad', 80);
        $posicion = nullable_string_local($jugador, 'posicion_principal', 30);
        $altura = nullable_int_local($jugador, 'altura_cm');
        $dorsal = nullable_int_local($jugador, 'dorsal');

        if ($altura !== null && ($altura < 130 || $altura > 230)) {
            $altura = null;
        }
        if ($dorsal !== null && ($dorsal < 0 || $dorsal > 99)) {
            $dorsal = null;
        }

        // 1) Resolver por ID externo, que es la vía principal.
        $stmt = $pdo->prepare("
            SELECT jugador_id
            FROM nucleo_ids_externos_jugador
            WHERE fuente = :fuente
              AND id_jugador_fuente = :id_fuente
            LIMIT 1
        ");
        $stmt->execute([
            ':fuente' => $fuente,
            ':id_fuente' => $idFuente,
        ]);
        $ext = $stmt->fetch();

        $jugadorId = $ext ? (int)$ext['jugador_id'] : null;

        // 2) Si es la primera vez que vemos el ID externo, intentar
        // reutilizar un jugador seed del mismo equipo por nombre normalizado.
        if ($jugadorId === null) {
            $clave = normalizar_nombre_jugador($nombre);
            $coincidencias = $mapaNombre[$clave] ?? [];

            if (count($coincidencias) === 1) {
                $jugadorId = (int)$coincidencias[0]['jugador_id'];
            } elseif (count($coincidencias) > 1 && $dorsal !== null) {
                $porDorsal = array_values(array_filter(
                    $coincidencias,
                    static fn(array $c): bool => $c['dorsal'] === $dorsal
                ));
                if (count($porDorsal) === 1) {
                    $jugadorId = (int)$porDorsal[0]['jugador_id'];
                }
            }
        }

        // 3) Crear si de verdad no existe.
        if ($jugadorId === null) {
            $stmt = $pdo->prepare("
                INSERT INTO nucleo_jugadores
                    (nombre_completo, fecha_nacimiento, nacionalidad,
                     altura_cm, posicion_principal)
                VALUES
                    (:nombre, :fecha_nacimiento, :nacionalidad,
                     :altura_cm, :posicion)
            ");
            $stmt->execute([
                ':nombre' => $nombre,
                ':fecha_nacimiento' => $fechaNacimiento,
                ':nacionalidad' => $nacionalidad,
                ':altura_cm' => $altura,
                ':posicion' => $posicion,
            ]);
            $jugadorId = (int)$pdo->lastInsertId();
            $creados++;
        } else {
            // Enriquecer sin borrar información buena existente.
            $stmt = $pdo->prepare("
                UPDATE nucleo_jugadores
                SET nombre_completo = CASE
                        WHEN CHAR_LENGTH(:nombre_cmp) > CHAR_LENGTH(nombre_completo)
                        THEN :nombre_set
                        ELSE nombre_completo
                    END,
                    fecha_nacimiento = COALESCE(fecha_nacimiento, :fecha_nacimiento),
                    nacionalidad = COALESCE(nacionalidad, :nacionalidad),
                    altura_cm = COALESCE(altura_cm, :altura_cm),
                    posicion_principal = COALESCE(posicion_principal, :posicion)
                WHERE jugador_id = :jugador_id
            ");
            $stmt->execute([
                ':nombre_cmp' => $nombre,
                ':nombre_set' => $nombre,
                ':fecha_nacimiento' => $fechaNacimiento,
                ':nacionalidad' => $nacionalidad,
                ':altura_cm' => $altura,
                ':posicion' => $posicion,
                ':jugador_id' => $jugadorId,
            ]);
            $actualizados++;
        }

        // Asociar ID externo si aún no existía.
        $stmt = $pdo->prepare("
            INSERT IGNORE INTO nucleo_ids_externos_jugador
                (jugador_id, fuente, id_jugador_fuente, confianza)
            VALUES
                (:jugador_id, :fuente, :id_fuente, 1.00)
        ");
        $stmt->execute([
            ':jugador_id' => $jugadorId,
            ':fuente' => $fuente,
            ':id_fuente' => $idFuente,
        ]);

        // Pertenencia actual.
        $stmt = $pdo->prepare("
            SELECT id
            FROM nucleo_pertenencias_jugador_equipo
            WHERE jugador_id = :jugador_id
              AND equipo_id = :equipo_id
              AND vigente_hasta IS NULL
            ORDER BY id DESC
            LIMIT 1
            FOR UPDATE
        ");
        $stmt->execute([
            ':jugador_id' => $jugadorId,
            ':equipo_id' => $equipoId,
        ]);
        $pertenencia = $stmt->fetch();

        if ($pertenencia) {
            $stmt = $pdo->prepare("
                UPDATE nucleo_pertenencias_jugador_equipo
                SET dorsal = COALESCE(:dorsal, dorsal),
                    fuente = :fuente,
                    obtenido_en = NOW()
                WHERE id = :id
            ");
            $stmt->execute([
                ':dorsal' => $dorsal,
                ':fuente' => $fuente,
                ':id' => (int)$pertenencia['id'],
            ]);
        } else {
            $stmt = $pdo->prepare("
                INSERT INTO nucleo_pertenencias_jugador_equipo
                    (jugador_id, equipo_id, dorsal, vigente_desde,
                     vigente_hasta, fuente, obtenido_en)
                VALUES
                    (:jugador_id, :equipo_id, :dorsal, :vigente_desde,
                     NULL, :fuente, NOW())
            ");
            $stmt->execute([
                ':jugador_id' => $jugadorId,
                ':equipo_id' => $equipoId,
                ':dorsal' => $dorsal,
                ':vigente_desde' => $fechaPlantilla,
                ':fuente' => $fuente,
            ]);
            $vinculosNuevos++;
        }

        $idsProcesados[$jugadorId] = true;
    }

    // Solo después de haber aceptado una plantilla de >=15 jugadores:
    // cerrar jugadores que la MISMA fuente ya no publica como activos.
    $ids = array_map('intval', array_keys($idsProcesados));

    if (count($ids) >= 15) {
        $placeholders = implode(',', array_fill(0, count($ids), '?'));

        $sql = "
            UPDATE nucleo_pertenencias_jugador_equipo
            SET vigente_hasta = ?,
                obtenido_en = NOW()
            WHERE equipo_id = ?
              AND vigente_hasta IS NULL
              AND fuente = ?
              AND jugador_id NOT IN ($placeholders)
        ";

        $params = array_merge(
            [$fechaPlantilla, $equipoId, $fuente],
            $ids
        );

        $stmt = $pdo->prepare($sql);
        $stmt->execute($params);
        $cerrados = $stmt->rowCount();
    } else {
        $cerrados = 0;
    }

    $pdo->commit();

    api_json([
        'ok' => true,
        'equipo_id' => $equipoId,
        'fuente' => $fuente,
        'jugadores_recibidos' => count($jugadores),
        'jugadores_creados' => $creados,
        'jugadores_actualizados' => $actualizados,
        'pertenencias_nuevas' => $vinculosNuevos,
        'pertenencias_cerradas' => $cerrados,
    ]);

} catch (Throwable $e) {
    if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) {
        $pdo->rollBack();
    }

    if ($e->getMessage() === 'EQUIPO_NO_EXISTE') {
        api_fail('equipo_id no existe.', 422);
    }
    if ($e->getMessage() === 'LOTE_NO_EXISTE') {
        api_fail('lote_id no existe.', 422);
    }

    error_log('guardar_plantilla.php: ' . $e->getMessage());
    api_fail('No se pudo guardar la plantilla.', 500);
}
