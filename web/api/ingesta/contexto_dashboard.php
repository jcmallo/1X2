<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

/**
 * Una jornada de quiniela con todo lo necesario para pintar el panel.
 *
 * Reúne en una sola respuesta lo que está repartido en cuatro tablas:
 *
 *   quiniela_jornadas            la jornada
 *   quiniela_partidos_jornada    las 15 casillas
 *   quiniela_probabilidades      lo que juega el público (LAE)
 *   mercado_cuotas_capturadas    la probabilidad de mercado, si el partido
 *                                está vinculado a nucleo_partidos
 *   optimizador_*                la última propuesta calculada
 *
 * Parámetros:
 *   temporada       etiqueta, p.ej. '2026-27' (opcional)
 *   numero_jornada  número concreto (opcional; si falta, la más reciente)
 *
 * Sobre las cuotas: se toma la captura más reciente de BetFair o Matchbook,
 * que son mercados de intercambio y cotizan con ~0,8% de margen frente al
 * 4,7% medio de las casas. Promediar todas las casas empeoraría la
 * estimación en lugar de mejorarla.
 */

$temporada = isset($_GET['temporada']) ? trim((string)$_GET['temporada']) : '';

$numeroJornada = null;
if (isset($_GET['numero_jornada']) && $_GET['numero_jornada'] !== '') {
    if (filter_var($_GET['numero_jornada'], FILTER_VALIDATE_INT) === false) {
        api_fail('numero_jornada debe ser entero.', 422);
    }
    $numeroJornada = (int)$_GET['numero_jornada'];
}

// --- La jornada ----------------------------------------------------------

$where = ' WHERE 1=1 ';
$params = [];
if ($temporada !== '') {
    $where .= ' AND j.etiqueta_temporada = :temporada ';
    $params[':temporada'] = $temporada;
}
if ($numeroJornada !== null) {
    $where .= ' AND j.numero_jornada = :numero ';
    $params[':numero'] = $numeroJornada;
}

try {
    $stmt = db()->prepare("
        SELECT jornada_id, numero_jornada, etiqueta_temporada,
               fecha_sorteo, publicado_en, fuente
        FROM quiniela_jornadas j
        $where
        ORDER BY etiqueta_temporada DESC, numero_jornada DESC
        LIMIT 1
    ");
    $stmt->execute($params);
    $jornada = $stmt->fetch();
} catch (Throwable $e) {
    error_log('contexto_dashboard.php (jornada): ' . $e->getMessage());
    api_fail('No se pudo consultar la jornada.', 500);
}

if ($jornada === false) {
    api_json([
        'ok' => true,
        'jornada' => null,
        'casillas' => [],
        'mensaje' => 'No hay ninguna jornada cargada con esos criterios.',
    ]);
}

$jornadaId = (int)$jornada['jornada_id'];

// --- Casillas, con el partido vinculado si lo hay ------------------------

try {
    $stmt = db()->prepare("
        SELECT
            pj.id AS partido_jornada_id,
            pj.posicion,
            pj.partido_id,
            pj.equipo_local_impreso,
            pj.equipo_visitante_impreso,
            pj.signo_oficial,
            pj.signo_pleno_oficial,
            p.fecha_hora_inicio,
            p.estado AS estado_partido,
            p.goles_local,
            p.goles_visitante,
            c.nombre AS competicion,
            c.genero
        FROM quiniela_partidos_jornada pj
        LEFT JOIN nucleo_partidos p ON p.partido_id = pj.partido_id
        LEFT JOIN nucleo_temporadas_competicion tc
            ON tc.temporada_competicion_id = p.temporada_competicion_id
        LEFT JOIN nucleo_competiciones c ON c.competicion_id = tc.competicion_id
        WHERE pj.jornada_id = :j
        ORDER BY pj.posicion
    ");
    $stmt->execute([':j' => $jornadaId]);
    $casillas = $stmt->fetchAll();
} catch (Throwable $e) {
    error_log('contexto_dashboard.php (casillas): ' . $e->getMessage());
    api_fail('No se pudieron consultar las casillas.', 500);
}

// --- Probabilidades: la captura más reciente de cada fuente --------------

try {
    $stmt = db()->prepare("
        SELECT q.posicion, q.fuente, q.tipo, q.prob_1, q.prob_x, q.prob_2,
               q.capturado_en, q.calidad
        FROM quiniela_probabilidades q
        JOIN (
            SELECT posicion, fuente, MAX(id) AS ultimo
            FROM quiniela_probabilidades
            WHERE jornada_id = :j
            GROUP BY posicion, fuente
        ) u ON u.ultimo = q.id
        WHERE q.jornada_id = :j2
        ORDER BY q.posicion, q.fuente
    ");
    $stmt->execute([':j' => $jornadaId, ':j2' => $jornadaId]);
    $probabilidades = $stmt->fetchAll();
} catch (Throwable $e) {
    error_log('contexto_dashboard.php (probabilidades): ' . $e->getMessage());
    api_fail('No se pudieron consultar las probabilidades.', 500);
}

$porCasilla = [];
foreach ($probabilidades as $p) {
    $porCasilla[(int)$p['posicion']][$p['fuente']] = [
        'tipo' => $p['tipo'],
        'p1' => $p['prob_1'] !== null ? (float)$p['prob_1'] : null,
        'px' => $p['prob_x'] !== null ? (float)$p['prob_x'] : null,
        'p2' => $p['prob_2'] !== null ? (float)$p['prob_2'] : null,
        'capturado_en' => $p['capturado_en'],
        'calidad' => $p['calidad'],
    ];
}

// --- Cuotas de mercado de los partidos vinculados ------------------------

$idsPartido = [];
foreach ($casillas as $c) {
    if ($c['partido_id'] !== null) {
        $idsPartido[] = (int)$c['partido_id'];
    }
}

$mercado = [];
if ($idsPartido !== []) {
    $ph = implode(',', array_fill(0, count($idsPartido), '?'));
    try {
        // Solo exchanges: cotizan sin margen de casa y son mejor estimador.
        $stmt = db()->prepare("
            SELECT m.partido_id, m.casa_apuestas, m.capturado_en,
                   m.franja_temporal,
                   m.prob_mercado_local, m.prob_mercado_empate,
                   m.prob_mercado_visitante
            FROM mercado_cuotas_capturadas m
            JOIN (
                SELECT partido_id, MAX(capturado_en) AS ultimo
                FROM mercado_cuotas_capturadas
                WHERE partido_id IN ($ph)
                GROUP BY partido_id
            ) u ON u.partido_id = m.partido_id AND u.ultimo = m.capturado_en
            -- Sin lista blanca de casas: cualquier fuente nueva entra sola.
            -- Filtrar por nombre obligaba a acordarse de añadir cada fuente
            -- al IN(), y ya falló dos veces dejando cuotas capturadas pero
            -- invisibles. Si hay empate a hora, gana el exchange, que cotiza
            -- con ~0,8% de margen frente al 6% de las casas al uso.
            GROUP BY m.partido_id, m.casa_apuestas, m.capturado_en,
                     m.franja_temporal, m.prob_mercado_local,
                     m.prob_mercado_empate, m.prob_mercado_visitante
            ORDER BY FIELD(m.casa_apuestas,
                           '1xbet', 'sportytrader_mediana',
                           'matchbook', 'betfair_ex_eu') DESC
        ");
        $stmt->execute($idsPartido);
        foreach ($stmt->fetchAll() as $m) {
            $pid = (int)$m['partido_id'];
            if (isset($mercado[$pid])) {
                continue;
            }
            $mercado[$pid] = [
                'casa' => $m['casa_apuestas'],
                'capturado_en' => $m['capturado_en'],
                'franja' => $m['franja_temporal'],
                'p1' => $m['prob_mercado_local'] !== null ? (float)$m['prob_mercado_local'] : null,
                'px' => $m['prob_mercado_empate'] !== null ? (float)$m['prob_mercado_empate'] : null,
                'p2' => $m['prob_mercado_visitante'] !== null ? (float)$m['prob_mercado_visitante'] : null,
            ];
        }
    } catch (Throwable $e) {
        error_log('contexto_dashboard.php (mercado): ' . $e->getMessage());
    }
}

// --- Última propuesta calculada -----------------------------------------

$propuesta = null;
try {
    $stmt = db()->prepare("
        SELECT ejecucion_id, objetivo, presupuesto_eur, creado_en
        FROM optimizador_ejecuciones
        WHERE jornada_id = :j
        ORDER BY ejecucion_id DESC
        LIMIT 1
    ");
    $stmt->execute([':j' => $jornadaId]);
    $ejecucion = $stmt->fetch();

    if ($ejecucion !== false) {
        $stmt = db()->prepare("
            SELECT s.partido_jornada_id, pj.posicion, s.recomendacion,
                   s.signos_incluidos
            FROM optimizador_selecciones_boleto s
            JOIN quiniela_partidos_jornada pj
                ON pj.id = s.partido_jornada_id
            WHERE s.ejecucion_id = :e
            ORDER BY pj.posicion
        ");
        $stmt->execute([':e' => (int)$ejecucion['ejecucion_id']]);

        $selecciones = [];
        foreach ($stmt->fetchAll() as $s) {
            $selecciones[(int)$s['posicion']] = [
                'recomendacion' => $s['recomendacion'],
                'signos' => json_decode($s['signos_incluidos'], true),
            ];
        }

        $propuesta = [
            'ejecucion_id' => (int)$ejecucion['ejecucion_id'],
            'objetivo' => $ejecucion['objetivo'],
            'presupuesto_eur' => $ejecucion['presupuesto_eur'] !== null
                ? (float)$ejecucion['presupuesto_eur'] : null,
            'creado_en' => $ejecucion['creado_en'],
            'selecciones' => $selecciones,
        ];
    }
} catch (Throwable $e) {
    error_log('contexto_dashboard.php (propuesta): ' . $e->getMessage());
}

// --- Pleno al 15 ---------------------------------------------------------

$pleno = [];
try {
    $stmt = db()->prepare("
        SELECT fuente, tipo, lado, prob_0, prob_1, prob_2, prob_m,
               es_estimado, capturado_en
        FROM quiniela_pleno15
        WHERE jornada_id = :j
        ORDER BY fuente, lado
    ");
    $stmt->execute([':j' => $jornadaId]);
    foreach ($stmt->fetchAll() as $p) {
        $pleno[$p['fuente']][strtolower($p['lado'])] = [
            'tipo' => $p['tipo'],
            'p0' => $p['prob_0'] !== null ? (float)$p['prob_0'] : null,
            'p1' => $p['prob_1'] !== null ? (float)$p['prob_1'] : null,
            'p2' => $p['prob_2'] !== null ? (float)$p['prob_2'] : null,
            'pm' => $p['prob_m'] !== null ? (float)$p['prob_m'] : null,
            'es_estimado' => (bool)$p['es_estimado'],
            'capturado_en' => $p['capturado_en'],
        ];
    }
} catch (Throwable $e) {
    // La tabla puede no existir todavía en una instalación antigua: el resto
    // del panel funciona igual sin el Pleno.
    error_log('contexto_dashboard.php (pleno): ' . $e->getMessage());
}

// --- Montaje -------------------------------------------------------------

$salida = [];
foreach ($casillas as $c) {
    $pos = (int)$c['posicion'];
    $pid = $c['partido_id'] !== null ? (int)$c['partido_id'] : null;

    $salida[] = [
        'partido_jornada_id' => (int)$c['partido_jornada_id'],
        'posicion' => $pos,
        'partido_id' => $pid,
        'local' => $c['equipo_local_impreso'],
        'visitante' => $c['equipo_visitante_impreso'],
        'fecha_hora_inicio' => $c['fecha_hora_inicio'],
        'competicion' => $c['competicion'],
        'genero' => $c['genero'],
        'estado_partido' => $c['estado_partido'],
        'goles_local' => $c['goles_local'] !== null ? (int)$c['goles_local'] : null,
        'goles_visitante' => $c['goles_visitante'] !== null ? (int)$c['goles_visitante'] : null,
        'signo_oficial' => $c['signo_oficial'],
        'signo_pleno_oficial' => $c['signo_pleno_oficial'],
        // (object) fuerza {} en lugar de []: PHP serializa un array vacío
        // como lista, y quien lo consuma espera un mapa por fuente.
        'probabilidades' => (object)($porCasilla[$pos] ?? []),
        'mercado' => ($pid !== null && isset($mercado[$pid])) ? $mercado[$pid] : null,
        'propuesta' => $propuesta['selecciones'][$pos] ?? null,
    ];
}

api_json([
    'ok' => true,
    'jornada' => [
        'jornada_id' => $jornadaId,
        'numero' => (int)$jornada['numero_jornada'],
        'temporada' => $jornada['etiqueta_temporada'],
        'fecha_sorteo' => $jornada['fecha_sorteo'],
        'fuente' => $jornada['fuente'],
    ],
    'propuesta' => $propuesta !== null ? [
        'ejecucion_id' => $propuesta['ejecucion_id'],
        'objetivo' => $propuesta['objetivo'],
        'presupuesto_eur' => $propuesta['presupuesto_eur'],
        'creado_en' => $propuesta['creado_en'],
    ] : null,
    'pleno15' => (object)$pleno,
    'casillas_vinculadas' => count($idsPartido),
    'casillas_con_mercado' => count($mercado),
    'casillas' => $salida,
]);
