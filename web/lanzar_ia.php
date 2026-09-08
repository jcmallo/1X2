<?php
declare(strict_types=1);

/**
 * Lanza el análisis por IA de una jornada. Una vez por jornada.
 *
 * POR QUÉ ESTE ARCHIVO EXISTE
 *
 * La clave de Anthropic vive en los secretos de GitHub, así que el análisis
 * lo ejecuta un workflow. Pero el momento de lanzarlo lo decide una persona
 * mirando el panel, no un reloj: el primer partido cambia de hora cada
 * semana y no siempre se está delante del ordenador cinco horas antes.
 *
 * Así que este archivo hace de puente: comprueba que se puede lanzar, echa
 * el candado y le pide a GitHub que ejecute el workflow.
 *
 * LAS TRES DEFENSAS, DE MENOS A MÁS FIABLE
 *
 *   1. La contraseña. Filtra a quien pase por la web por casualidad.
 *   2. La ventana de 12 horas. Fuera de ella no hay nada que analizar.
 *   3. La clave única de ia_lanzamientos. Esta es la que de verdad importa:
 *      es la base de datos la que impide el segundo lanzamiento, de forma
 *      atómica. Dos pestañas y dos clics a la vez se saltan las otras dos.
 *
 * El gasto máximo por jornada es un análisis, y eso no depende de que la
 * contraseña siga siendo secreta.
 */

require __DIR__ . '/api/ingesta/bootstrap.php';

$config = load_config();
date_default_timezone_set($config['timezone'] ?? 'Europe/Madrid');

// Desde cuántas horas antes del primer partido se puede lanzar.
//
// A 12 horas ya se han dado casi todas las ruedas de prensa y los partes
// médicos están publicados. Lo que todavía no hay son alineaciones probables
// ni previsión del tiempo fiable: eso llega sobre las 5 horas. Se abre a 12
// porque un análisis bueno que no llegas a jugar vale cero.
const VENTANA_HORAS = 12.0;

// Si un lanzamiento se queda a medias —el workflow falla, GitHub se cae—, el
// candado se libera pasado este tiempo. Sin esto, un fallo dejaría la jornada
// bloqueada para siempre.
const CADUCA_MINUTOS = 30;


function volver(string $estado, string $detalle = ''): void
{
    $destino = 'index.php?ia=' . rawurlencode($estado);
    if ($detalle !== '') {
        $destino .= '&msg=' . rawurlencode($detalle);
    }
    header('Location: ' . $destino, true, 303);
    exit;
}


if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    volver('error', 'Solo por POST.');
}

$rutaConfig = __DIR__ . '/config_ia.php';
if (!is_file($rutaConfig)) {
    volver('error', 'Falta config_ia.php en el servidor.');
}
$ia = require $rutaConfig;

// hash_equals compara en tiempo constante. Con == se podría averiguar la
// clave carácter a carácter midiendo lo que tarda en responder.
$clave = (string)($_POST['clave'] ?? '');
if ($clave === '' || !hash_equals((string)($ia['clave'] ?? ''), $clave)) {
    volver('clave');
}

$jornadaId = isset($_POST['jornada_id']) ? (int)$_POST['jornada_id'] : 0;
if ($jornadaId <= 0) {
    volver('error', 'Jornada no indicada.');
}

try {
    $pdo = db();

    // --- ¿Estamos dentro de la ventana? ---------------------------------

    $stmt = $pdo->prepare("
        SELECT MIN(p.fecha_hora_inicio) AS primero
        FROM quiniela_partidos_jornada q
        JOIN partidos p ON p.id = q.partido_id
        WHERE q.jornada_id = :j AND p.fecha_hora_inicio IS NOT NULL
    ");
    $stmt->execute([':j' => $jornadaId]);
    $primero = $stmt->fetchColumn();

    if (!$primero) {
        volver('error', 'La jornada aún no tiene horarios: falta vincular el boleto.');
    }

    $horas = (strtotime((string)$primero) - time()) / 3600;
    if ($horas > VENTANA_HORAS) {
        volver('pronto', sprintf('Faltan %.0f horas para el primer partido.', $horas));
    }
    if ($horas <= 0) {
        volver('tarde', 'La jornada ya ha empezado.');
    }

    // --- El candado -----------------------------------------------------
    //
    // Un lanzamiento anterior que se quedó colgado se limpia antes de
    // intentarlo; uno terminado no se toca nunca.

    $pdo->prepare("
        DELETE FROM ia_lanzamientos
        WHERE jornada_id = :j
          AND estado IN ('en_cola', 'analizando', 'error')
          AND solicitado_en < (NOW() - INTERVAL :m MINUTE)
    ")->execute([':j' => $jornadaId, ':m' => CADUCA_MINUTOS]);

    try {
        $stmt = $pdo->prepare("
            INSERT INTO ia_lanzamientos (jornada_id, estado, horas_antes)
            VALUES (:j, 'en_cola', :h)
        ");
        $stmt->execute([':j' => $jornadaId, ':h' => round($horas, 2)]);
        $solicitudId = (int)$pdo->lastInsertId();
    } catch (PDOException $e) {
        // 23000 es violación de clave única: ya hay un lanzamiento.
        if ($e->getCode() === '23000') {
            $stmt = $pdo->prepare("
                SELECT estado FROM ia_lanzamientos WHERE jornada_id = :j
            ");
            $stmt->execute([':j' => $jornadaId]);
            $estado = (string)$stmt->fetchColumn();
            volver($estado === 'hecho' ? 'ya_hecho' : 'en_marcha');
        }
        throw $e;
    }

    // --- Pedirle a GitHub que ejecute el workflow ------------------------

    $gh = $ia['github'] ?? [];
    $url = sprintf(
        'https://api.github.com/repos/%s/%s/actions/workflows/%s/dispatches',
        rawurlencode((string)$gh['propietario']),
        rawurlencode((string)$gh['repositorio']),
        rawurlencode((string)$gh['workflow'])
    );

    $cuerpo = json_encode([
        'ref' => (string)($gh['rama'] ?? 'main'),
        'inputs' => [
            // Los inputs de GitHub son siempre cadenas, incluso los números.
            'solicitud' => (string)$solicitudId,
            'forzar' => 'true',
        ],
    ], JSON_UNESCAPED_SLASHES);

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => $cuerpo,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 20,
        CURLOPT_HTTPHEADER => [
            'Accept: application/vnd.github+json',
            'Authorization: Bearer ' . (string)$gh['token'],
            'X-GitHub-Api-Version: 2022-11-28',
            'Content-Type: application/json',
            // GitHub rechaza las peticiones sin User-Agent.
            'User-Agent: Predictor1X2',
        ],
    ]);
    $respuesta = curl_exec($ch);
    $codigo = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $errorCurl = curl_error($ch);
    curl_close($ch);

    // 204 sin contenido es el éxito. Cualquier otra cosa deja el candado
    // suelto: no tendría sentido bloquear la jornada por un análisis que no
    // ha llegado a arrancar.
    if ($codigo !== 204) {
        $pdo->prepare("DELETE FROM ia_lanzamientos WHERE id = :i")
            ->execute([':i' => $solicitudId]);

        if ($codigo === 401 || $codigo === 403) {
            $pista = 'GitHub rechaza el token: comprueba que no ha caducado y '
                . 'que tiene permiso Actions: Read and write.';
        } elseif ($codigo === 404) {
            $pista = 'GitHub no encuentra el workflow: revisa propietario, '
                . 'repositorio y nombre del archivo en config_ia.php.';
        } elseif ($codigo === 422) {
            $pista = 'GitHub acepta el workflow pero no los datos: la rama o '
                . 'los inputs no cuadran.';
        } elseif ($codigo === 0) {
            $pista = 'No se pudo contactar con GitHub: ' . $errorCurl;
        } else {
            $pista = 'GitHub respondió ' . $codigo;
        }
        error_log('lanzar_ia.php: HTTP ' . $codigo . ' — ' . (string)$respuesta);
        volver('error', $pista);
    }

    volver('lanzada');
} catch (Throwable $e) {
    error_log('lanzar_ia.php: ' . $e->getMessage());
    volver('error', 'Error del servidor; mira el log.');
}
