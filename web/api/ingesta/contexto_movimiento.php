<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

/**
 * Cómo se han movido las cuotas y los porcentajes de una jornada.
 *
 * POR QUÉ HACE FALTA
 *
 * contexto_dashboard.php devuelve una foto: la última captura de cada
 * partido. Para decidir qué marcar basta, pero deja fuera la pregunta más
 * útil que se le puede hacer a esta base de datos:
 *
 *     ¿esta noticia ya estaba en el precio cuando se capturó la cuota?
 *
 * Si un delantero titular cae lesionado a las 11:08 y la cuota del local
 * pasa del 47% al 41% a las 12:00, el mercado ya lo ha descontado. Un
 * análisis que a las 13:45 "descubra" esa lesión y rebaje otra vez al local
 * está contando dos veces la misma información.
 *
 * Con el movimiento delante, esa doble contabilidad se puede evitar: lo que
 * ya movió el precio no vuelve a moverlo.
 *
 * Hay hasta 54 capturas por partido, así que se devuelven agrupadas por
 * franja temporal en lugar de una a una.
 *
 * Parámetros:
 *   temporada       etiqueta, p.ej. '2026-27' (opcional)
 *   numero_jornada  número concreto (opcional; si falta, la más reciente)
 */

$temporada = isset($_GET['temporada']) ? trim((string)$_GET['temporada']) : '';

$numeroJornada = null;
if (isset($_GET['numero_jornada']) && $_GET['numero_jornada'] !== '') {
    if (filter_var($_GET['numero_jornada'], FILTER_VALIDATE_INT) === false) {
        api_fail('numero_jornada debe ser entero.', 422);
    }
    $numeroJornada = (int)$_GET['numero_jornada'];
}

try {
    $pdo = db();

    $where = ' WHERE 1=1 ';
    $params = [];
    if ($temporada !== '') {
        $where .= ' AND etiqueta_temporada = :t ';
        $params[':t'] = $temporada;
    }
    if ($numeroJornada !== null) {
        $where .= ' AND numero_jornada = :n ';
        $params[':n'] = $numeroJornada;
    }

    $stmt = $pdo->prepare("
        SELECT jornada_id, numero_jornada, etiqueta_temporada
        FROM quiniela_jornadas $where
        ORDER BY etiqueta_temporada DESC, numero_jornada DESC LIMIT 1
    ");
    $stmt->execute($params);
    $jornada = $stmt->fetch();

    if ($jornada === false) {
        api_json(['ok' => true, 'jornada' => null, 'casillas' => []]);
    }

    $jid = (int)$jornada['jornada_id'];

    $stmt = $pdo->prepare("
        SELECT posicion, partido_id, equipo_local_impreso, equipo_visitante_impreso
        FROM quiniela_partidos_jornada WHERE jornada_id = :j ORDER BY posicion
    ");
    $stmt->execute([':j' => $jid]);
    $casillas = $stmt->fetchAll();

    // --- Movimiento del mercado, resumido por franja ----------------------
    //
    // De cada franja se toma la captura más reciente: dentro de una misma
    // franja las cuotas apenas se mueven, y devolver las 54 llenaría la
    // respuesta de ruido.

    $ids = [];
    foreach ($casillas as $c) {
        if ($c['partido_id'] !== null) {
            $ids[] = (int)$c['partido_id'];
        }
    }

    $mercado = [];
    if ($ids !== []) {
        $ph = implode(',', array_fill(0, count($ids), '?'));
        $stmt = $pdo->prepare("
            SELECT m.partido_id, m.franja_temporal, m.casa_apuestas, m.capturado_en,
                   m.prob_mercado_local, m.prob_mercado_empate, m.prob_mercado_visitante
            FROM mercado_cuotas_capturadas m
            JOIN (
                SELECT partido_id, franja_temporal, MAX(capturado_en) AS ultimo
                FROM mercado_cuotas_capturadas
                WHERE partido_id IN ($ph)
                GROUP BY partido_id, franja_temporal
            ) u ON u.partido_id = m.partido_id
               AND u.franja_temporal <=> m.franja_temporal
               AND u.ultimo = m.capturado_en
            ORDER BY m.partido_id, m.capturado_en
        ");
        $stmt->execute($ids);
        foreach ($stmt->fetchAll() as $r) {
            $mercado[(int)$r['partido_id']][] = [
                'franja' => $r['franja_temporal'],
                'casa' => $r['casa_apuestas'],
                'capturado_en' => $r['capturado_en'],
                'p1' => $r['prob_mercado_local'] !== null ? (float)$r['prob_mercado_local'] : null,
                'px' => $r['prob_mercado_empate'] !== null ? (float)$r['prob_mercado_empate'] : null,
                'p2' => $r['prob_mercado_visitante'] !== null ? (float)$r['prob_mercado_visitante'] : null,
            ];
        }
    }

    // --- Movimiento del público, también por franja -----------------------
    //
    // Lo que juega la gente evoluciona distinto del mercado: el público
    // reacciona más tarde y más fuerte. Comparar las dos curvas dice si el
    // público está descubriendo algo o simplemente siguiendo al favorito.

    $stmt = $pdo->prepare("
        SELECT q.posicion, q.tipo, q.fuente, q.prob_1, q.prob_x, q.prob_2, q.capturado_en
        FROM quiniela_probabilidades q
        JOIN (
            SELECT posicion, tipo, MAX(id) AS ultimo
            FROM quiniela_probabilidades
            WHERE jornada_id = :j AND fuente LIKE 'LAE%'
            GROUP BY posicion, tipo
        ) u ON u.ultimo = q.id
        WHERE q.jornada_id = :j2
        ORDER BY q.posicion, q.capturado_en
    ");
    $stmt->execute([':j' => $jid, ':j2' => $jid]);
    $publico = [];
    foreach ($stmt->fetchAll() as $r) {
        $publico[(int)$r['posicion']][] = [
            'franja' => $r['tipo'],
            'fuente' => $r['fuente'],
            'capturado_en' => $r['capturado_en'],
            'p1' => $r['prob_1'] !== null ? (float)$r['prob_1'] : null,
            'px' => $r['prob_x'] !== null ? (float)$r['prob_x'] : null,
            'p2' => $r['prob_2'] !== null ? (float)$r['prob_2'] : null,
        ];
    }

    $salida = [];
    foreach ($casillas as $c) {
        $pos = (int)$c['posicion'];
        $pid = $c['partido_id'] !== null ? (int)$c['partido_id'] : null;
        $salida[] = [
            'posicion' => $pos,
            'local' => $c['equipo_local_impreso'],
            'visitante' => $c['equipo_visitante_impreso'],
            'mercado' => $pid !== null ? ($mercado[$pid] ?? []) : [],
            'publico' => $publico[$pos] ?? [],
        ];
    }

    api_json([
        'ok' => true,
        'jornada' => [
            'numero' => (int)$jornada['numero_jornada'],
            'temporada' => $jornada['etiqueta_temporada'],
        ],
        'casillas' => $salida,
    ]);
} catch (Throwable $e) {
    error_log('contexto_movimiento.php: ' . $e->getMessage());
    api_fail('No se pudo consultar el movimiento.', 500);
}
