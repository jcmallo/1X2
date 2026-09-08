<?php
declare(strict_types=1);

/**
 * Panel de La Quiniela.
 *
 * Se pinta en el servidor a propósito. La alternativa habitual (JavaScript
 * llamando a la API) obligaría a poner el token de ingesta en el navegador,
 * donde cualquiera puede leerlo viendo el código fuente de la página. Aquí
 * se consulta con db() y el token no interviene: solo hace falta para
 * escribir desde GitHub.
 *
 * Lee las mismas tablas que contexto_dashboard.php. No lo llama por HTTP
 * porque sería pedirle al servidor que se conecte a sí mismo para leer una
 * base de datos que ya tiene delante.
 */

require __DIR__ . '/api/ingesta/bootstrap.php';
require __DIR__ . '/optimizador.php';

$config = load_config();
date_default_timezone_set($config['timezone'] ?? 'Europe/Madrid');

// Se prefiere la versión con transparencia, pero si no está subida se usa
// el original: así la cabecera nunca sale rota por el orden de subida.
$LOGO = null;
foreach (['imagenes/logo_predictor_1x2_transparente.png',
          'imagenes/logo_predictor_1x2.png'] as $cand) {
    if (is_file(__DIR__ . '/' . $cand)) {
        $LOGO = $cand;
        break;
    }
}

// Presupuesto pedido, en columnas. Las flechas mandan ?cols= con el valor
// ya ajustado a la escalera de apuestas válidas.
$colsPedidas = isset($_GET['cols']) ? (int)$_GET['cols'] : 0;
$recalcular = $colsPedidas > 0;

$SIGNOS = ['1', 'X', '2'];
$CATEGORIAS = ['0', '1', '2', 'M'];
$PRECIO_COLUMNA = 0.75;

// El desplegable manda ?j=2026-27/4. Se aceptan también los parámetros
// sueltos, que son más cómodos de escribir a mano.
$temporada = isset($_GET['temporada']) ? trim((string)$_GET['temporada']) : '';
$numero = isset($_GET['jornada']) && $_GET['jornada'] !== ''
    ? filter_var($_GET['jornada'], FILTER_VALIDATE_INT)
    : null;

if (isset($_GET['j']) && preg_match('~^([\w-]{1,20})/(\d{1,3})$~', (string)$_GET['j'], $m)) {
    $temporada = $m[1];
    $numero = (int)$m[2];
}

if ($numero === false) {
    $numero = null;
}

$error = null;
$jornada = null;
$casillas = [];
$pleno = [];
$plenoMercado = [];
$plenoModelo = [];
$propuesta = null;
$jornadasDisponibles = [];

try {
    $pdo = db();

    // --- La jornada ------------------------------------------------------

    $where = ' WHERE 1=1 ';
    $params = [];
    if ($temporada !== '') {
        $where .= ' AND etiqueta_temporada = :t ';
        $params[':t'] = $temporada;
    }
    if ($numero !== null) {
        $where .= ' AND numero_jornada = :n ';
        $params[':n'] = $numero;
    }

    $stmt = $pdo->prepare("
        SELECT jornada_id, numero_jornada, etiqueta_temporada, fecha_sorteo
        FROM quiniela_jornadas
        $where
        ORDER BY etiqueta_temporada DESC, numero_jornada DESC
        LIMIT 1
    ");
    $stmt->execute($params);
    $jornada = $stmt->fetch();

    if ($jornada !== false) {
        $jid = (int)$jornada['jornada_id'];

        // --- Casillas ----------------------------------------------------

        $stmt = $pdo->prepare("
            SELECT pj.id AS pj_id, pj.posicion, pj.partido_id,
                   pj.equipo_local_impreso, pj.equipo_visitante_impreso,
                   pj.signo_oficial,
                   -- El resultado del Pleno. Faltaba en esta lista, así que
                   -- $plenoOficial era siempre null y la fila 15 nunca
                   -- enseñaba el resultado, ni en la jornada recién jugada
                   -- ni en las del histórico.
                   pj.signo_pleno_oficial,
                   p.fecha_hora_inicio, p.estado, p.goles_local, p.goles_visitante,
                   c.nombre AS competicion, c.genero
            FROM quiniela_partidos_jornada pj
            LEFT JOIN nucleo_partidos p ON p.partido_id = pj.partido_id
            LEFT JOIN nucleo_temporadas_competicion tc
                ON tc.temporada_competicion_id = p.temporada_competicion_id
            LEFT JOIN nucleo_competiciones c ON c.competicion_id = tc.competicion_id
            WHERE pj.jornada_id = :j
            ORDER BY pj.posicion
        ");
        $stmt->execute([':j' => $jid]);
        $casillas = $stmt->fetchAll();

        // --- Porcentajes del público -------------------------------------

        $stmt = $pdo->prepare("
            SELECT q.posicion, q.fuente, q.tipo, q.prob_1, q.prob_x, q.prob_2,
                   q.capturado_en
            FROM quiniela_probabilidades q
            JOIN (
                SELECT posicion, fuente, MAX(id) AS ultimo
                FROM quiniela_probabilidades WHERE jornada_id = :j
                GROUP BY posicion, fuente
            ) u ON u.ultimo = q.id
            WHERE q.jornada_id = :j2
        ");
        $stmt->execute([':j' => $jid, ':j2' => $jid]);

        // Se guardan todas las fuentes por casilla: LAE (lo que juega la
        // gente) y MODELO_PROPIO (nuestro pronóstico) son columnas
        // distintas del panel, no versiones del mismo dato.
        $porFuente = [];
        foreach ($stmt->fetchAll() as $r) {
            $porFuente[(int)$r['posicion']][$r['fuente']] = $r;
        }

        $publico = [];
        $modelo = [];
        foreach ($porFuente as $pos => $fuentes) {
            // Los porcentajes de cierre mandan sobre los estimados.
            $publico[$pos] = $fuentes['LAE_CIERRE'] ?? $fuentes['LAE_ESTIMADO'] ?? null;
            $modelo[$pos] = $fuentes['MODELO_PROPIO'] ?? null;
        }

        // --- Mercado ------------------------------------------------------

        $ids = array_values(array_filter(array_map(
            static fn($c) => $c['partido_id'] !== null ? (int)$c['partido_id'] : null,
            $casillas
        )));

        $mercado = [];
        if ($ids !== []) {
            $ph = implode(',', array_fill(0, count($ids), '?'));
            $stmt = $pdo->prepare("
                SELECT m.partido_id, m.casa_apuestas, m.capturado_en,
                       m.prob_mercado_local, m.prob_mercado_empate,
                       m.prob_mercado_visitante
                FROM mercado_cuotas_capturadas m
                JOIN (
                    SELECT partido_id, MAX(capturado_en) AS ultimo
                    FROM mercado_cuotas_capturadas
                    WHERE partido_id IN ($ph)
                    GROUP BY partido_id
                ) u ON u.partido_id = m.partido_id AND u.ultimo = m.capturado_en
                -- Sin lista blanca de casas: cualquier fuente nueva entra
                -- sola. Si un partido tiene varias capturas a la misma hora,
                -- gana el exchange, que cotiza con menos margen.
                ORDER BY FIELD(m.casa_apuestas,
                               '1xbet', 'sportytrader_mediana',
                               'matchbook', 'betfair_ex_eu') DESC
            ");
            $stmt->execute($ids);
            foreach ($stmt->fetchAll() as $r) {
                $pid = (int)$r['partido_id'];
                if (!isset($mercado[$pid])) {
                    $mercado[$pid] = $r;
                }
            }
        }

        // --- Pleno al 15 ---------------------------------------------------

        try {
            $stmt = $pdo->prepare("
                SELECT fuente, lado, prob_0, prob_1, prob_2, prob_m
                FROM quiniela_pleno15 WHERE jornada_id = :j
                ORDER BY FIELD(fuente,'LAE_CIERRE','LAE_ESTIMADO','MERCADO')
            ");
            $stmt->execute([':j' => $jid]);
            foreach ($stmt->fetchAll() as $r) {
                $lado = strtolower($r['lado']);
                if ($r['fuente'] === 'MERCADO') {
                    // Cada fuente en su columna: el mercado dice qué va a
                    // pasar, LAE qué juega la gente y el modelo qué creemos
                    // nosotros. Mezclarlas impediría compararlas después.
                    $plenoMercado[$lado] ??= $r;
                } elseif ($r['fuente'] === 'MODELO_PROPIO') {
                    $plenoModelo[$lado] ??= $r;
                } elseif (!isset($pleno[$lado])) {
                    $pleno[$lado] = $r;
                }
            }
        } catch (Throwable $e) {
            // La tabla puede no existir en una instalación antigua.
        }

        // --- Última propuesta ----------------------------------------------

        $stmt = $pdo->prepare("
            SELECT ejecucion_id, objetivo, presupuesto_eur, creado_en, congelada
            FROM optimizador_ejecuciones WHERE jornada_id = :j
            ORDER BY ejecucion_id DESC LIMIT 1
        ");
        $stmt->execute([':j' => $jid]);
        $ejec = $stmt->fetch();

        if ($ejec !== false) {
            $stmt = $pdo->prepare("
                SELECT pj.posicion, s.recomendacion, s.signos_incluidos
                FROM optimizador_selecciones_boleto s
                JOIN quiniela_partidos_jornada pj ON pj.id = s.partido_jornada_id
                WHERE s.ejecucion_id = :e
            ");
            $stmt->execute([':e' => (int)$ejec['ejecucion_id']]);
            $sel = [];
            foreach ($stmt->fetchAll() as $r) {
                $signos = json_decode((string)$r['signos_incluidos'], true);
                $sel[(int)$r['posicion']] = is_array($signos) ? $signos : [];
            }
            $propuesta = ['meta' => $ejec, 'selecciones' => $sel];
        }
    }

    // Para el selector de jornadas.
    // Se trae también la combinación ganadora para que el desplegable
    // distinga de un vistazo las jornadas ya resueltas de las pendientes.
    // Solo las 14 primeras: la 15 es el Pleno y no se juega a 1/X/2.
    $stmt = $pdo->query("
        SELECT j.numero_jornada, j.etiqueta_temporada,
               GROUP_CONCAT(
                   CASE WHEN pj.posicion <= 14 THEN pj.signo_oficial END
                   ORDER BY pj.posicion SEPARATOR ''
               ) AS combinacion
        FROM quiniela_jornadas j
        LEFT JOIN quiniela_partidos_jornada pj ON pj.jornada_id = j.jornada_id
        GROUP BY j.jornada_id, j.numero_jornada, j.etiqueta_temporada
        ORDER BY j.etiqueta_temporada DESC, j.numero_jornada DESC
        LIMIT 40
    ");
    $jornadasDisponibles = $stmt->fetchAll();
} catch (Throwable $e) {
    error_log('index.php: ' . $e->getMessage());
    $error = 'No se pudieron leer los datos.';
}


// --- Estado del análisis por IA ------------------------------------------
//
// Tres cosas que decide este bloque:
//
//   cuánto falta para el primer partido  → el contador y si el botón se abre
//   si hay un análisis pedido o hecho    → qué enseña el panel
//   si la propuesta está congelada       → si se puede seguir tocando
//
// Todo con SELECT: aquí no se lanza nada. Lanzar es cosa de lanzar_ia.php,
// que es POST y pide contraseña.

const IA_VENTANA_HORAS = 12.0;

$primerPartido = null;     // timestamp del primer partido de la jornada
$horasAlCierre = null;     // lo que falta, en horas
$iaEstado = null;          // en_cola | analizando | hecho | error
$iaAnalisis = null;        // el análisis guardado, si lo hay
$congelada = false;

if ($jornada && !$error) {
    try {
        $pdo = db();
        $jid = (int)$jornada['jornada_id'];

        foreach (($casillas ?? []) as $c) {
            if (!empty($c['fecha_hora_inicio'])) {
                $t = strtotime((string)$c['fecha_hora_inicio']);
                if ($t && ($primerPartido === null || $t < $primerPartido)) {
                    $primerPartido = $t;
                }
            }
        }
        if ($primerPartido !== null) {
            $horasAlCierre = ($primerPartido - time()) / 3600;
        }

        // Las tablas pueden no existir todavía: el panel tiene que seguir
        // funcionando igual, solo que sin botón.
        try {
            $stmt = $pdo->prepare("
                SELECT estado, solicitado_en FROM ia_lanzamientos
                WHERE jornada_id = :j LIMIT 1
            ");
            $stmt->execute([':j' => $jid]);
            $fila = $stmt->fetch();
            if ($fila !== false) {
                $iaEstado = (string)$fila['estado'];
                // Un workflow que falla deja la fila en 'en_cola' para
                // siempre. Sin esto el panel se quedaría recargándose solo
                // hasta el día del juicio.
                $edad = time() - strtotime((string)$fila['solicitado_en']);
                if ($iaEstado !== 'hecho' && $edad > 30 * 60) {
                    $iaEstado = 'caducado';
                }
            }

            $stmt = $pdo->prepare("
                SELECT analisis, ajustes, senales, pleno, coste_usd, creado_en
                FROM ia_analisis_jornada WHERE jornada_id = :j
                ORDER BY id DESC LIMIT 1
            ");
            $stmt->execute([':j' => $jid]);
            $fila = $stmt->fetch();
            if ($fila !== false) {
                $iaAnalisis = $fila;
                // El análisis guardado manda sobre la fila de lanzamiento: si
                // está en la base, el análisis existe aunque el candado se
                // haya quedado a medias.
                if ($iaEstado !== 'hecho') {
                    $iaEstado = 'hecho';
                }
            }
        } catch (Throwable $e) {
            // Sin tablas de IA no hay botón, y ya está.
        }

        $congelada = isset($propuesta['meta']['congelada'])
            && (int)$propuesta['meta']['congelada'] === 1;
    } catch (Throwable $e) {
        error_log('index.php (estado IA): ' . $e->getMessage());
    }
}

// Los ajustes que sobrevivieron a la segunda opinión, por casilla.
$iaAjustes = [];
if ($iaAnalisis && !empty($iaAnalisis['ajustes'])) {
    $obj = json_decode((string)$iaAnalisis['ajustes'], true);
    foreach (($obj['ajustes'] ?? []) as $a) {
        if (isset($a['posicion'], $a['signo_despues'])) {
            $iaAjustes[(int)$a['posicion']] = $a;
        }
    }
}

// Una propuesta congelada no se recalcula por mucho que lo pida la URL.
if ($congelada && ($_GET['descongelar'] ?? '') !== '1') {
    $recalcular = false;
}

// Descongelar es gratis: no vuelve a llamar a la IA, solo suelta la propuesta
// para poder repartir el presupuesto otra vez. Hace falta porque el análisis
// se equivoca, y quedarse atado a una propuesta que no convence sería peor
// que no congelar nada.
if ($congelada && ($_GET['descongelar'] ?? '') === '1' && $jornada) {
    try {
        db()->prepare("
            UPDATE optimizador_ejecuciones SET congelada = 0
            WHERE ejecucion_id = :e
        ")->execute([':e' => (int)$propuesta['meta']['ejecucion_id']]);
        header('Location: ' . $_SERVER['PHP_SELF'] . '?j='
            . rawurlencode($jornada['etiqueta_temporada'] . '/'
                . (int)$jornada['numero_jornada']), true, 303);
        exit;
    } catch (Throwable $e) {
        error_log('index.php (descongelar): ' . $e->getMessage());
    }
}


// --- Recálculo en vivo ---------------------------------------------------
//
// Cuando se cambia el presupuesto no se va y se vuelve a GitHub Actions: se
// recalcula aquí con el mismo criterio que usa optimizador/cartera.py. La
// propuesta guardada en la base no se toca; esto es una simulación de "qué
// saldría si jugara X columnas".

$propuestaViva = null;
if ($recalcular && $casillas && isset($publico, $mercado)) {
    $paraCalcular = [];
    foreach ($casillas as $c) {
        $pos = (int)$c['posicion'];
        if ($pos === 15) {
            continue;
        }
        $pub = terna($publico[$pos] ?? null, 'prob_1', 'prob_x', 'prob_2');
        if (!$pub) {
            continue;   // sin saber qué juega la gente no hay valor que calcular
        }
        $pid = $c['partido_id'] !== null ? (int)$c['partido_id'] : null;
        $paraCalcular[$pos] = [
            'pub' => $pub,
            'prob' => probabilidad_casilla(
                $c, $pub, $pid, $mercado ?? [], $modelo ?? []
            ),
        ];
    }
    if ($paraCalcular) {
        $propuestaViva = construir_cartera($paraCalcular, $colsPedidas);
    }
}

// --- Propuesta del Pleno al 15 -------------------------------------------
//
// El Pleno no entra en construir_cartera porque no se juega a 1/X/2 sino a
// goles, y abrirlo multiplicaría el coste del boleto entero. Se decide
// aparte y por lados: en cada uno se marca la categoría más probable según
// la mejor fuente disponible.
//
// El orden de preferencia importa: el mercado dice qué va a pasar y LAE
// solo qué juega la gente. Donde haya cuotas, mandan.

$plenoPropuesta = [];
foreach (['local', 'visitante'] as $lado) {
    // Orden de preferencia: el mercado primero, luego el modelo propio y
    // por último lo que juega la gente. Los dos primeros dicen qué va a
    // pasar; el tercero solo qué apuesta el público.
    $fuente = $plenoMercado[$lado] ?? $plenoModelo[$lado] ?? $pleno[$lado] ?? null;
    if (!$fuente) {
        continue;
    }
    $mejor = null;
    $mejorValor = -1.0;
    foreach ($CATEGORIAS as $cat) {
        $k = ['0' => 'prob_0', '1' => 'prob_1', '2' => 'prob_2', 'M' => 'prob_m'][$cat];
        $v = (float)($fuente[$k] ?? 0);
        if ($v > $mejorValor) {
            $mejorValor = $v;
            $mejor = $cat;
        }
    }
    if ($mejor !== null) {
        $plenoPropuesta[$lado] = [
            'categoria' => $mejor,
            'probabilidad' => $mejorValor,
            'origen' => isset($plenoMercado[$lado]) ? 'mercado'
                : (isset($plenoModelo[$lado]) ? 'modelo' : 'público'),
        ];
    }
}

// --- Guardar lo calculado ------------------------------------------------
//
// El botón Calcular no se limita a simular: deja la propuesta guardada, que
// es lo que hace que "calculada a las..." signifique algo. Se escribe
// directamente con db() en lugar de llamar al endpoint por HTTP, porque el
// token de ingesta no puede salir al navegador.

/**
 * Guarda una propuesta y devuelve el id de la ejecución.
 *
 * Se escribe con db() en lugar de llamar al endpoint por HTTP porque el
 * token de ingesta no puede salir al navegador.
 *
 * $congelar cierra el boleto: a partir de ahí recargar la página no lo
 * cambia. Solo lo usa el paso que aplica el análisis de la IA.
 */
function guardar_propuesta(int $jornadaId, array $cartera, bool $congelar = false): int
{
    $pdo = db();
    $pdo->beginTransaction();
    try {
        $stmt = $pdo->prepare("
            INSERT INTO optimizador_ejecuciones
                (jornada_id, objetivo, presupuesto_eur, congelada)
            VALUES (:j, 'valor_esperado', :p, :c)
        ");
        $stmt->execute([
            ':j' => $jornadaId,
            ':p' => round($cartera['coste'], 2),
            ':c' => $congelar ? 1 : 0,
        ]);
        $ejecucionId = (int)$pdo->lastInsertId();

        $stmt = $pdo->prepare("
            SELECT posicion, id FROM quiniela_partidos_jornada WHERE jornada_id = :j
        ");
        $stmt->execute([':j' => $jornadaId]);
        $idsCasilla = [];
        foreach ($stmt->fetchAll() as $fila) {
            $idsCasilla[(int)$fila['posicion']] = (int)$fila['id'];
        }

        $ins = $pdo->prepare("
            INSERT INTO optimizador_selecciones_boleto
                (ejecucion_id, partido_jornada_id, recomendacion, signos_incluidos)
            VALUES (:e, :pj, :r, :s)
        ");
        foreach ($cartera['selecciones'] as $pos => $signos) {
            if (!isset($idsCasilla[$pos]) || !$signos) {
                continue;
            }
            $ins->execute([
                ':e' => $ejecucionId,
                ':pj' => $idsCasilla[$pos],
                ':r' => ['FIJO', 'DOBLE', 'TRIPLE'][min(count($signos), 3) - 1],
                ':s' => json_encode(array_values($signos), JSON_UNESCAPED_UNICODE),
            ]);
        }

        $pdo->commit();
        return $ejecucionId;
    } catch (Throwable $e) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        throw $e;
    }
}


// --- Guardar lo calculado ------------------------------------------------

$guardado = null;
if ($recalcular && $propuestaViva && ($_GET['guardar'] ?? '') === '1' && $jornada) {
    try {
        $guardado = guardar_propuesta(
            (int)$jornada['jornada_id'], $propuestaViva
        );
        // Se relee para que la cabecera muestre la fecha recién guardada.
        $propuesta = [
            'meta' => [
                'creado_en' => date('Y-m-d H:i:s'),
                'ejecucion_id' => $guardado,
                'presupuesto_eur' => $propuestaViva['coste'],
                'congelada' => 0,
            ],
            'selecciones' => $propuestaViva['selecciones'],
        ];
    } catch (Throwable $e) {
        error_log('index.php (guardar): ' . $e->getMessage());
        $guardado = -1;
    }
}


// --- Aplicar el análisis de la IA y cerrar el boleto ----------------------
//
// Cuando el análisis ha terminado y la propuesta todavía no está congelada,
// se rehace una vez con los ajustes que sobrevivieron a la segunda opinión y
// se cierra. Pasa solo, al recargar la página: no hay que pulsar nada.
//
// Los ajustes se aplican sustituyendo signos, no rehaciendo el reparto: la
// IA dice "en la 12 juega X en vez de 2", no "gasta el presupuesto de otra
// manera". Cuántos dobles y triples caben lo sigue decidiendo el valor.

$iaAplicada = false;
if ($jornada && $iaEstado === 'hecho' && !$congelada && $casillas
    && isset($publico, $mercado)) {
    try {
        $cols = $propuesta
            ? (int)round(((float)$propuesta['meta']['presupuesto_eur']) / $PRECIO_COLUMNA)
            : 8;

        $paraCalcular = [];
        foreach ($casillas as $c) {
            $pos = (int)$c['posicion'];
            if ($pos === 15) {
                continue;
            }
            $pub = terna($publico[$pos] ?? null, 'prob_1', 'prob_x', 'prob_2');
            if (!$pub) {
                continue;
            }
            $pid = $c['partido_id'] !== null ? (int)$c['partido_id'] : null;
            $paraCalcular[$pos] = [
                'pub' => $pub,
                'prob' => probabilidad_casilla(
                $c, $pub, $pid, $mercado ?? [], $modelo ?? []
            ),
            ];
        }

        if ($paraCalcular) {
            $cartera = construir_cartera($paraCalcular, max($cols, 1));

            foreach ($iaAjustes as $pos => $a) {
                if (!isset($cartera['selecciones'][$pos])) {
                    continue;
                }
                $signos = $cartera['selecciones'][$pos];
                $antes = (string)($a['signo_antes'] ?? '');
                $despues = (string)$a['signo_despues'];

                if (count($signos) === 1) {
                    // Fijo: se cambia por el que dice la IA.
                    $cartera['selecciones'][$pos] = [$despues];
                } elseif (in_array($antes, $signos, true)) {
                    // Doble o triple: se sustituye el signo dentro del grupo
                    // para no alterar cuántas columnas cuesta la casilla.
                    $signos[array_search($antes, $signos, true)] = $despues;
                    $cartera['selecciones'][$pos] = array_values(
                        array_unique($signos)
                    );
                }
            }

            $ejec = guardar_propuesta(
                (int)$jornada['jornada_id'], $cartera, true
            );
            $propuesta = [
                'meta' => [
                    'creado_en' => date('Y-m-d H:i:s'),
                    'ejecucion_id' => $ejec,
                    'presupuesto_eur' => $cartera['coste'],
                    'congelada' => 1,
                ],
                'selecciones' => $cartera['selecciones'],
            ];
            $congelada = true;
            $iaAplicada = true;
            $recalcular = false;
            $propuestaViva = null;
        }
    } catch (Throwable $e) {
        error_log('index.php (aplicar IA): ' . $e->getMessage());
    }
}


// --- Ayudas de presentación ------------------------------------------------

/** Normaliza una terna a fracciones que suman 1. */
/**
 * De dónde sale la probabilidad de una casilla, por orden de preferencia.
 *
 * PUERTO de _probabilidad() en optimizador/proponer.py. Los dos tienen que
 * decir lo mismo, o el panel y GitHub propondrán boletos distintos.
 *
 * LIGA F VA AL REVÉS QUE LAS DEMÁS, y no por capricho:
 *
 *   · Su "mercado" es un precio de 1xBet que no se mueve en toda la semana.
 *     Medido: las cuatro capturas de una jornada salen idénticas. Eso no es
 *     un mercado con dinero detrás informando el precio, es un número
 *     puesto ahí.
 *
 *   · Y es donde nuestro modelo va mejor: 62,5% de acierto y +0,2344 sobre
 *     el baseline, el mejor de las tres competiciones.
 *
 * En LaLiga y Segunda al contrario: el mercado acierta el 52,7% y nosotros
 * el 51,4%, así que manda el mercado.
 *
 * Y si no hay ninguna de las dos, se devuelve null y la casilla se resuelve
 * siguiendo al público.
 */
function probabilidad_casilla(
    array $c, array $pub, ?int $pid, array $mercado, array $modelo
): ?array {
    $mer = terna(
        $pid !== null ? ($mercado[$pid] ?? null) : null,
        'prob_mercado_local', 'prob_mercado_empate', 'prob_mercado_visitante'
    );
    $mod = terna($modelo[(int)$c['posicion']] ?? null,
                 'prob_1', 'prob_x', 'prob_2');

    $local = (string)($c['equipo_local_impreso'] ?? $c['local'] ?? '');
    $visitante = (string)($c['equipo_visitante_impreso'] ?? $c['visitante'] ?? '');

    // El género viene de la propia tabla de competiciones, así que es más
    // fiable que buscar 'liga f' en un nombre que puede cambiar.
    $femenino = strtoupper((string)($c['genero'] ?? '')) === 'F'
        || es_liga_f($c['competicion'] ?? null, $local, $visitante);

    if ($femenino) {
        if ($mod !== null) { return $mod; }
        if ($mer !== null) { return $mer; }
    } else {
        if ($mer !== null) { return $mer; }
        if ($mod !== null) { return $mod; }
    }
    // Sin mercado ni modelo no hay probabilidad: la casilla se resuelve
    // siguiendo al público, como siempre. Aquí hubo una curva que convertía
    // los porcentajes de LAE en probabilidad; se quitó porque nunca llegaba
    // a usarse y cuando entraba aplanaba los repartos hasta hacer que un
    // Barça femenino pareciera un partido abierto.
    return null;
}


/**
 * La categoría de goles de un lado del Pleno.
 *
 * El campo llega en dos formatos distintos, y los dos están en la base:
 *
 *     'M2', '11', '0M'      importador histórico: dos caracteres seguidos
 *     'M-2', '1-1'          cerrar_jornada.py: con guion en medio
 *
 * Se aceptan los dos en lugar de elegir uno y migrar 686 jornadas. Un
 * formato equivocado devuelve null y la fila se queda sin marcar, que es
 * mejor que marcar una categoría que no es.
 */
function pleno_lado(?string $valor, string $lado): ?string
{
    $v = trim((string)$valor);
    if ($v === '') {
        return null;
    }
    $partes = strpos($v, '-') !== false
        ? explode('-', $v)
        : str_split($v);
    if (count($partes) < 2) {
        return null;
    }
    $cat = strtoupper(trim($partes[$lado === 'local' ? 0 : 1]));
    return in_array($cat, ['0', '1', '2', 'M'], true) ? $cat : null;
}


function terna(?array $fila, string $k1, string $kx, string $k2): ?array
{
    if (!$fila) {
        return null;
    }
    foreach ([$k1, $kx, $k2] as $k) {
        if (!isset($fila[$k]) || $fila[$k] === null) {
            return null;
        }
    }
    $v = ['1' => (float)$fila[$k1], 'X' => (float)$fila[$kx], '2' => (float)$fila[$k2]];
    $t = array_sum($v);
    if ($t <= 0) {
        return null;
    }
    return array_map(static fn($x) => $x / $t, $v);
}

/**
 * Valor de un signo: probabilidad de mercado dividida por cuánta gente lo juega.
 *
 * Por encima de 1 el signo está infrajugado: acierta más veces de las que se
 * juega, y como el premio se reparte entre acertantes, paga más.
 */
function valor(?array $prob, ?array $pub, string $s): ?float
{
    if (!$prob || !$pub) {
        return null;
    }
    return $prob[$s] / max($pub[$s], 0.005);
}

/**
 * Desviación del mercado respecto al público, en puntos porcentuales.
 *
 * Es la señal que de verdad importa y por eso va marcada con flecha: dice
 * si la gente está jugando de más o de menos ese signo. El umbral de 3 pp
 * evita marcar ruido de redondeo.
 */
function flecha(?array $mer, ?array $pub, string $s): array
{
    if (!$mer || !$pub) {
        return ['', ''];
    }
    $d = ($mer[$s] - $pub[$s]) * 100;
    if ($d >= 3) {
        return ['▲', 'sube'];   // el mercado le da más: infrajugado
    }
    if ($d <= -3) {
        return ['▼', 'baja'];   // el mercado le da menos: sobrejugado
    }
    return ['', ''];
}

function claseValor(?float $v): string
{
    if ($v === null) {
        return '';
    }
    if ($v >= 1.15) {
        return 'v-alto';
    }
    if ($v < 0.90) {
        return 'v-bajo';
    }
    return '';
}

const DIAS = ['DOM', 'LUN', 'MAR', 'MIÉ', 'JUE', 'VIE', 'SÁB'];

function horario(?string $iso): array
{
    if (!$iso) {
        return ['', ''];
    }
    try {
        $d = new DateTime($iso);
    } catch (Throwable $e) {
        return ['', ''];
    }
    return [DIAS[(int)$d->format('w')], $d->format('H:i')];
}

function e(?string $s): string
{
    return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8');
}

// Coste del boleto propuesto.
$combinaciones = 1;
$dobles = 0;
if ($propuesta) {
    foreach ($propuesta['selecciones'] as $p => $sg) {
        $combinaciones *= max(1, count($sg));
        if ($p !== 15 && count($sg) > 1) {
            $dobles++;
        }
    }
}

// Una jornada con signos oficiales ya está jugada: ahí lo que interesa ver
// es el resultado, no lo que se propuso. La columna cambia de nombre y de
// contenido en consecuencia.
$resueltas = 0;
$plenoOficial = null;
foreach ($casillas as $c) {
    if ((int)$c['posicion'] <= 14 && ($c['signo_oficial'] ?? null)) {
        $resueltas++;
    }
    if ((int)$c['posicion'] === 15) {
        $plenoOficial = $c['signo_pleno_oficial'] ?? null;
    }
}
$jugada = $resueltas >= 14;

$conMercado = 0;
foreach ($casillas as $c) {
    if ($c['partido_id'] !== null && isset($mercado[(int)$c['partido_id']])) {
        $conMercado++;
    }
}
?>
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Predictor 1X2</title>
<link rel="icon" type="image/png" sizes="32x32" href="imagenes/icono-32.png">
<link rel="apple-touch-icon" sizes="180x180" href="imagenes/icono-180.png">
<meta name="theme-color" content="#00306c">
<style>
/*
 * Esta tabla se consulta varias veces por semana y se escanea de un vistazo,
 * así que no lleva animaciones de entrada: a esa frecuencia el movimiento
 * estorba en lugar de ayudar. Lo único que se mueve es el hover, que es
 * respuesta directa a la mano del usuario.
 *
 * El trabajo de diseño está en la densidad y la alineación: cifras tabulares
 * para que las columnas de porcentajes formen rejilla vertical, alturas de
 * fila cortas, y una sola señal de color por celda.
 */
/* Azul y rojo salen del logo (#00306c y #e42424) para que la marca y la
   interfaz no vayan cada una por su lado. El rojo del logo se oscurece un
   punto para texto pequeño: a ese tamaño satura demasiado y cuesta leerlo. */
:root {
  --fondo:#fbfbfc; --panel:#fff; --borde:#e6e8ec; --borde-suave:#f0f1f4;
  --texto:#15181d; --suave:#5f6672; --tenue:#98a0ac;
  --marca:#00306c; --marca-fondo:#e9eff8; --marca-borde:#b0c2dc;
  --sube:#12764a; --baja:#c62828;
  --acento:#b8860b;
  --fila-hover:#f6f8fb;
  --radio:5px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --fondo:#101215; --panel:#171a1f; --borde:#262a31; --borde-suave:#1e2228;
    --texto:#e8eaee; --suave:#98a1ad; --tenue:#69717d;
    /* El azul del logo es demasiado oscuro sobre fondo negro: se aclara
       manteniendo el tono, no se sustituye por otro azul. */
    --marca:#6ba3ea; --marca-fondo:#0f2340; --marca-borde:#2a4a78;
    --sube:#3ecf8e; --baja:#ef6a5c;
    --acento:#d9a520;
    --fila-hover:#1c2027;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--fondo);color:var(--texto);
  font:13px/1.35 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
}
.envoltura{max-width:960px;margin:0 auto;padding:18px 12px 40px}

/* Cabecera compacta: una sola línea con todo lo importante */
.cabecera{
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  margin-bottom:9px;padding-bottom:9px;
  border-bottom:1px solid var(--borde);
}
/* El logo preside la página: centrado y grande. Se declaran width y height
   en el HTML para que el navegador reserve el hueco antes de descargarlo y
   nada salte al cargar. */
.marca{
  display:flex;justify-content:center;
  padding:6px 0 18px;
}
/* El logo vuelve a la portada. Baja apenas al pulsarlo: es el único gesto
   de la página y conviene que responda a la mano. */
.enlace-marca{
  display:block;line-height:0;border-radius:6px;
  transition:transform 120ms cubic-bezier(.23,1,.32,1), opacity 120ms ease;
}
.enlace-marca:active{transform:scale(.985)}
@media (hover:hover) and (pointer:fine){
  .enlace-marca:hover{opacity:.88}
}
.enlace-marca:focus-visible{
  outline:2px solid var(--marca);outline-offset:6px;
}
.logo{
  height:auto;width:min(427px,72vw);
  display:block;
}
.cabecera h1{
  font-size:16px;font-weight:650;margin:0;letter-spacing:-.015em;
}
/* El azul del logo (#00306c) es casi invisible sobre fondo oscuro. Se aclara
   con un filtro en vez de mantener otro fichero: sube el brillo conservando
   los tonos, así el rojo sigue siendo rojo y el blanco del balón no se toca. */
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]) .logo{filter:brightness(2.1) saturate(1.15)}
}
.cabecera .meta{font-size:11.5px;color:var(--tenue)}
.cabecera .ok{color:var(--sube);font-weight:600}
.cabecera .mal{color:var(--baja);font-weight:600}
/* Mientras se toquetea el presupuesto lo que se ve es una simulación: hasta
   pulsar Calcular no queda guardada, y conviene que se note. */
.cabecera .aviso-sim{color:var(--acento);font-weight:600}
.cabecera .sep{color:var(--borde)}
.espaciador{flex:1}

.cifras-clave{display:flex;gap:14px;align-items:baseline;font-size:11.5px;color:var(--tenue)}

/* Presupuesto: las flechas no suman de euro en euro, saltan al siguiente
   número de columnas que se puede jugar de verdad (2^dobles x 3^triples). */
.presupuesto{display:flex;align-items:center;gap:6px}
.presupuesto .paso{
  width:24px;height:24px;line-height:22px;text-align:center;
  border:1px solid var(--borde);border-radius:6px;background:var(--panel);
  color:var(--texto);text-decoration:none;font-size:15px;font-weight:600;
  transition:border-color 120ms ease, transform 100ms cubic-bezier(.23,1,.32,1);
}
.presupuesto .paso:active{transform:scale(.94)}
@media (hover:hover) and (pointer:fine){
  .presupuesto .paso:hover{border-color:var(--marca);color:var(--marca)}
}
.presupuesto .importe{
  min-width:104px;text-align:center;line-height:1.15;
  font-variant-numeric:tabular-nums;
}
.presupuesto .importe b{font-size:14px;font-weight:650}
.presupuesto .importe i{
  display:block;font-style:normal;font-size:9.5px;color:var(--tenue);
  white-space:nowrap;
}
.presupuesto .calcular{
  margin-left:4px;padding:5px 12px;border-radius:6px;
  background:var(--marca);color:#fff;text-decoration:none;
  font-size:11.5px;font-weight:600;letter-spacing:.01em;
  transition:transform 100ms cubic-bezier(.23,1,.32,1), opacity 120ms ease;
}
.presupuesto .calcular:active{transform:scale(.97)}
@media (hover:hover) and (pointer:fine){
  .presupuesto .calcular:hover{opacity:.9}
}

/* --- El análisis por IA --------------------------------------------------
   Se apoya en las mismas variables que el resto del panel para que herede
   el modo oscuro sin repetir colores. */
.ia{
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin-left:10px;font-size:11px;
}
.ia .cuenta{
  line-height:1.15;text-align:right;font-variant-numeric:tabular-nums;
}
.ia .cuenta b{display:block;font-size:13px;font-weight:650}
.ia .cuenta i{
  display:block;font-style:normal;font-size:9.5px;color:var(--tenue);
  white-space:nowrap;
}
.ia-estado{
  padding:3px 8px;border-radius:5px;white-space:nowrap;
  border:1px solid var(--borde);color:var(--tenue);
}
.ia-estado.listo{border-color:var(--marca);color:var(--marca);font-weight:600}
.ia-estado.cerrado{
  background:var(--marca);border-color:var(--marca);color:#fff;font-weight:600;
}
/* El punto que late dice que hay algo pasando sin robar atención. */
.ia-estado.trabajando::before{
  content:"";display:inline-block;width:6px;height:6px;margin-right:5px;
  border-radius:50%;background:var(--marca);vertical-align:middle;
  animation:latido 1.4s ease-in-out infinite;
}
@keyframes latido{0%,100%{opacity:.25}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){
  .ia-estado.trabajando::before{animation:none;opacity:.7}
}
.ia-abrir{color:var(--tenue);text-decoration:underline;font-size:10px}
.ia-nota{color:var(--tenue);font-size:9.5px;max-width:150px;line-height:1.2}
.ia-form{display:flex;align-items:center;gap:5px}
.ia-form input{
  font:inherit;font-size:11px;width:74px;padding:4px 7px;
  border:1px solid var(--borde);border-radius:5px;
  background:var(--fondo);color:var(--texto);
}
.ia-form button{
  font:inherit;font-size:11px;font-weight:600;padding:5px 11px;
  border:0;border-radius:6px;cursor:pointer;
  background:var(--marca);color:#fff;
  transition:transform 100ms cubic-bezier(.23,1,.32,1), opacity 120ms ease;
}
.ia-form button:active{transform:scale(.97)}
@media (hover:hover) and (pointer:fine){
  .ia-form button:hover{opacity:.9}
}
.ia-aviso{
  flex-basis:100%;margin-top:5px;padding:5px 9px;border-radius:5px;
  font-size:11px;border:1px solid var(--borde);
}
.ia-aviso.ok{border-color:var(--marca);color:var(--marca)}
.ia-aviso.mal{border-color:#b00020;color:#b00020}
.ia-aviso.aviso{color:var(--tenue)}
.cifras-clave b{
  color:var(--texto);font-weight:650;font-size:13px;
  font-variant-numeric:tabular-nums;margin-right:3px;
}

select{
  font:inherit;font-size:12px;padding:4px 24px 4px 8px;border-radius:var(--radio);
  border:1px solid var(--borde);background:var(--panel);color:var(--texto);
  cursor:pointer;appearance:none;
  background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath fill='%2398a0ac' d='M0 0h10L5 6z'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 8px center;
  transition:border-color 120ms ease, transform 100ms cubic-bezier(.23,1,.32,1);
}
select:hover{border-color:var(--tenue)}
select:active{transform:scale(.98)}
select:focus-visible{outline:2px solid var(--marca);outline-offset:1px}

.panel{
  background:var(--panel);border:1px solid var(--borde);
  border-radius:8px;overflow:hidden;
}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:760px}

thead th{
  font-size:9.5px;font-weight:600;color:var(--tenue);
  text-transform:uppercase;letter-spacing:.06em;
  padding:7px 6px 6px;border-bottom:1px solid var(--borde);
  white-space:nowrap;text-align:center;
}
thead th.izq{text-align:left}
/* Los tres grupos de cifras comparten ancho para que las columnas de 1, X
   y 2 queden alineadas verticalmente entre público y mercado. */
thead th.grupo{width:108px}

tbody td{
  padding:0 6px;border-bottom:1px solid var(--borde-suave);
  height:30px;vertical-align:middle;
}
tbody tr:last-child td{border-bottom:0}
@media (hover:hover) and (pointer:fine){
  tbody tr.partido:hover{background:var(--fila-hover)}
}

.pos{
  width:22px;text-align:right;color:var(--tenue);
  font-size:11px;font-variant-numeric:tabular-nums;padding-right:8px;
}

/* Día y hora apilados: ocupa una línea de alto en vez de dos columnas */
.hora{width:46px;line-height:1.15;text-align:center}
.hora .d{
  display:block;font-size:9px;font-weight:650;color:var(--tenue);letter-spacing:.04em;
}
.hora .h{
  display:block;font-size:11px;color:var(--suave);
  font-variant-numeric:tabular-nums;
}

.equipos{
  font-size:12.5px;font-weight:500;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:1px;width:38%;
}
.equipos .g{color:var(--tenue);font-weight:400;margin:0 3px}
.equipos .f{
  font-size:8.5px;font-weight:600;color:var(--acento);
  border:1px solid currentColor;border-radius:3px;
  padding:0 3px;margin-left:5px;vertical-align:1px;letter-spacing:.03em;
}

/* El boleto: tres casillas como las del impreso */
.boleto{width:80px;padding:0 4px!important}
.boleto .caja{display:flex;gap:2px;justify-content:center}
.boleto i{
  flex:1;max-width:23px;height:19px;line-height:18px;
  border:1px solid var(--borde);border-radius:3px;
  font-style:normal;font-size:10.5px;font-weight:600;
  text-align:center;color:var(--tenue);background:transparent;
}
.boleto i.on{
  background:var(--marca-fondo);border-color:var(--marca-borde);
  color:var(--marca);
}
/* En una jornada ya jugada, el resultado va marcado y lo que propusimos se
   subraya en verde o rojo según acertara. Se usa el borde inferior y no el
   fondo para no competir con la marca del resultado. */
.boleto i.acierto{box-shadow:inset 0 -2px 0 var(--sube)}
.boleto i.fallo{box-shadow:inset 0 -2px 0 var(--baja);opacity:.75}

/* Rejilla de porcentajes: tres cifras de ancho fijo */
.pct{width:108px}
.pct .g{display:flex}
.pct span{
  flex:1;text-align:center;font-size:11.5px;
  font-variant-numeric:tabular-nums;color:var(--suave);
  white-space:nowrap;
}
.pct span.nulo{color:var(--borde)}
.pct.mer span{color:var(--texto)}
/* El pronóstico propio se distingue con el color de la marca: es lo único
   de la tabla que calculamos nosotros. */
.pct.pred span{color:var(--marca);font-weight:600}
.pct.pred span.nulo{color:var(--borde);font-weight:400}
.pct em{
  font-style:normal;font-size:7.5px;vertical-align:1.5px;margin-left:1px;
}
.pct .sube em{color:var(--sube)}
.pct .baja em{color:var(--baja)}
.pct .sube{color:var(--sube);font-weight:600}
.pct .baja{color:var(--baja)}

.val{
  width:52px;text-align:right;padding-right:12px!important;
  font-size:11.5px;font-variant-numeric:tabular-nums;color:var(--suave);
}
.v-alto{color:var(--sube);font-weight:650}
.v-bajo{color:var(--baja)}

/* Pleno: dos filas dentro de la misma tabla, sin bloque aparte */
tr.pleno td{background:var(--fondo)}
tr.pleno-1 td{border-bottom-color:transparent}
tr.pleno td.pos{color:var(--tenue)}
.boleto.p15 i{max-width:17px;font-size:9.5px}
.pct.p15 .g{max-width:96px}

.aviso{
  font-size:11px;color:var(--suave);padding:7px 10px;
  border-top:1px solid var(--borde);background:var(--fondo);
  display:flex;gap:6px;align-items:flex-start;line-height:1.45;
}
.aviso b{color:var(--texto);font-weight:600}

footer{margin-top:12px;font-size:10.5px;color:var(--tenue);line-height:1.6}
.leyenda{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:7px}
.leyenda span{display:flex;align-items:center;gap:4px}
.mini{
  width:15px;height:12px;line-height:11px;border-radius:2px;font-size:8px;
  font-weight:600;text-align:center;display:inline-block;
  border:1px solid var(--marca-borde);background:var(--marca-fondo);color:var(--marca);
}
footer p{margin:0 0 4px}

.vacio{padding:28px 16px;text-align:center;color:var(--suave);font-size:12.5px}

@media (max-width:560px){
  .envoltura{padding:12px 8px 32px}
  .cifras-clave{gap:10px}
}


/* ================================================================
   ESTILO BOLETO CLÁSICO · solo presentación, sin tocar la lógica PHP
   ================================================================ */
:root{
  --quiniela:#b51f2e;
  --quiniela-oscuro:#8f1722;
  --quiniela-linea:#e3aeb4;
  --quiniela-pastel:#fff1f2;
  --quiniela-pastel-2:#fff8f8;
  --papel:#fffdfc;
  --papel-fondo:#f7f4f3;
  --grupo-primera:#c84d59;
  --grupo-segunda:#6f8eae;
  --grupo-femenina:#a86f8a;
  --grupo-pleno:#8f1722;
}

/* Papel cálido, como el boleto físico, pero sin perder limpieza de pantalla. */
body{background:var(--papel-fondo)}
.panel{
  background:var(--papel);
  border-color:#dfd4d2;
  box-shadow:0 2px 10px rgba(87,35,39,.045);
}

/* Cabecera de la jornada: rojo quiniela; el predictor conserva el azul propio. */
.cabecera{border-bottom-color:#e3c6c9}
.cabecera h1{color:var(--quiniela-oscuro);font-weight:750}
.presupuesto .calcular{background:var(--quiniela)}
/* Con su media query, como la regla original: en táctil el :hover se queda
   pegado después de tocar y el botón parece activo cuando no lo está. */
@media (hover:hover) and (pointer:fine){
  .presupuesto .paso:hover{border-color:var(--quiniela);color:var(--quiniela)}
}
select:focus-visible{outline-color:var(--quiniela)}

/* Cabecera de tabla inspirada en el impreso: rosa muy suave + tipografía roja. */
thead th{
  background:#f8e1e3;
  color:#8f4e54;
  border-bottom:1px solid #d9a5aa;
}
thead th.izq{color:var(--quiniela-oscuro)}

/* Zebra clásico: blanco / rojo pastel / blanco / rojo pastel... */
tbody tr.partido:nth-child(odd) td{background:#fff}
tbody tr.partido:nth-child(even) td{background:var(--quiniela-pastel)}

/* El hover sigue siendo muy ligero para no romper el patrón del boleto. */
@media (hover:hover) and (pointer:fine){
  tbody tr.partido:hover td{background:#fbe3e5}
}

/* Agrupación visual sin añadir HTML:
   1-7 Primera · 8-10 Segunda · 11-14 Liga F. */
tbody tr.partido:nth-child(-n+7) td.pos{border-left:4px solid var(--grupo-primera)}
tbody tr.partido:nth-child(n+8):nth-child(-n+10) td.pos{border-left:4px solid var(--grupo-segunda)}
tbody tr.partido:nth-child(n+11):nth-child(-n+14) td.pos{border-left:4px solid var(--grupo-femenina)}

/* Separadores de bloque: la tabla sigue siendo continua, pero se leen tres grupos. */
tbody tr.partido:nth-child(8) td{
  border-top:5px solid #dfe8f1;
}
tbody tr.partido:nth-child(11) td{
  border-top:5px solid #f0dde6;
}

/* Número de partido con más presencia, como en el boleto. */
.pos{
  color:#9a5960;
  font-weight:700;
  padding-left:5px!important;
}

/* Rejilla 1-X-2: borde rojo de boleto y selección roja sólida. */
.boleto i{
  border-color:#d8a3a8;
  background:rgba(255,255,255,.72);
  color:#ad7479;
  border-radius:2px;
}
.boleto i.on{
  background:var(--quiniela);
  border-color:var(--quiniela-oscuro);
  color:#fff;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.18);
}

/* Las columnas estadísticas quedan más 'de panel' y menos 'de formulario'. */
.pct span{color:#5d5f65}
.pct.mer span{color:#2c2f35}
.pct.pred span{color:#00306c}
.val{font-weight:550}

/* Liga F: la F mantiene su función pero se integra con el bloque. */
.equipos .f{
  color:#8f506d;
  border-color:#c997ad;
  background:#fff7fa;
}

/* Pleno al 15: bloque propio, más reconocible, sin cambiar su estructura de dos filas. */
tr.pleno td{
  background:#f9e3e5;
  border-bottom-color:#edc8cc;
}
tr.pleno-1 td{
  border-top:6px solid #d7a0a6;
  border-bottom-color:transparent;
}
tr.pleno td.pos{
  color:var(--grupo-pleno);
  border-left:4px solid var(--grupo-pleno);
  font-weight:800;
}
tr.pleno .equipos{font-weight:650;color:#5d2329}
tr.pleno .boleto i{background:#fff8f8}
tr.pleno .boleto i.on{background:var(--quiniela-oscuro);color:#fff}

/* Avisos y pie en tono papel para que el boleto siga siendo el protagonista. */
.aviso{background:#fbf6f5;border-top-color:#e5d3d1}
footer{color:#7a6b6a}   /* #8d7e7d daba 3,55 de contraste; a 10,5px se queda corto */

/* Ajuste vertical compacto: sube el logo y gana altura útil en pantallas de portátil.
   No cambia tamaños ni lógica; solo elimina aire superior y reduce el hueco bajo el logo. */
.envoltura{padding-top:0}
.marca{padding-top:0;padding-bottom:10px}

/* Variante oscura: conserva los grupos sin convertir los pasteles en manchas claras. */
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --papel-fondo:#101215;
    --papel:#171a1f;
    --quiniela-pastel:#2a1c1f;
  }
  :root:not([data-theme="light"]) body{background:#101215}
  :root:not([data-theme="light"]) .panel{background:#171a1f;border-color:#3a2c2f}
  :root:not([data-theme="light"]) thead th{background:#2a1b1f;color:#dba8ae;border-bottom-color:#644047}
  :root:not([data-theme="light"]) thead th.izq{color:#efb8be}
  :root:not([data-theme="light"]) tbody tr.partido:nth-child(odd) td{background:#171a1f}
  :root:not([data-theme="light"]) tbody tr.partido:nth-child(even) td{background:#21191b}
  :root:not([data-theme="light"]) tbody tr.partido:nth-child(8) td{border-top-color:#263544}
  :root:not([data-theme="light"]) tbody tr.partido:nth-child(11) td{border-top-color:#3a2831}
  :root:not([data-theme="light"]) tr.pleno td{background:#28191d}
  /* Las casillas SIN marcar. El :not(.on) es necesario: sin él esta regla
     pisaba el fondo rojo de las marcadas y el boleto parecía vacío en
     modo oscuro aunque los signos estuvieran puestos.

     El motivo es de especificidad, no de orden. Esta regla vale (0,3,1)
     por el :root, el :not([data-theme]) y el .boleto; la que pinta la
     selección, `.boleto i.on`, solo (0,2,1). Ganaba esta. */
  :root:not([data-theme="light"]) .boleto i:not(.on){
    background:#1d1c20;border-color:#70434a;color:#c99da2;
  }

  /* Y la selección, repetida aquí con la especificidad del bloque oscuro
     (0,4,1) para que gane con seguridad. Con el :not(.on) de arriba ya
     bastaría, pero así el rojo sobrevive aunque alguien reordene el CSS. */
  :root:not([data-theme="light"]) .boleto i.on{
    background:var(--quiniela);
    border-color:#e0576a;
    color:#fff;
  }
  /* El Pleno usa el rojo oscuro en claro, que sobre fondo negro se
     apagaría demasiado. Aquí lleva el mismo rojo que el resto. */
  :root:not([data-theme="light"]) tr.pleno .boleto i.on{
    background:var(--quiniela);
    border-color:#e0576a;
    color:#fff;
  }
  :root:not([data-theme="light"]) .equipos .f{background:#241a1f}
}
</style>
</head>
<body>
<div class="envoltura">

<?php if ($error): ?>
  <div class="panel"><div class="vacio"><b>Error.</b> <?= e($error) ?> Revisa el log del servidor.</div></div>

<?php elseif (!$jornada): ?>
  <div class="panel"><div class="vacio">No hay ninguna jornada cargada. Lanza el workflow «Capturar porcentajes de LAE».</div></div>

<?php else: ?>

  <?php if ($LOGO): ?>
    <div class="marca">
      <a href="." class="enlace-marca" aria-label="Predictor 1X2 · ir a la jornada actual">
        <img class="logo" src="<?= e($LOGO) ?>" width="2508" height="627"
             alt="Predictor 1X2 · Múltiples y Reducidas">
      </a>
    </div>
  <?php endif; ?>

  <div class="cabecera">
    <h1>Jornada <?= (int)$jornada['numero_jornada'] ?></h1>
    <span class="meta"><?= e($jornada['etiqueta_temporada']) ?></span>
    <span class="sep">·</span>
    <span class="meta">
      <?php if ($jugada): ?>
        jornada jugada<?= $propuesta ? ' · propuesta subrayada en verde si acertó' : '' ?>
      <?php elseif ($guardado > 0): ?>
        <span class="ok">guardada ahora mismo</span>
      <?php elseif ($guardado === -1): ?>
        <span class="mal">no se pudo guardar · mira el log</span>
      <?php elseif ($propuestaViva): ?>
        <span class="aviso-sim">simulación sin guardar</span>
      <?php elseif ($propuesta): ?>
        calculada <?= e(date('d/m H:i', strtotime((string)$propuesta['meta']['creado_en']))) ?>
      <?php else: ?>
        sin propuesta
      <?php endif; ?>
    </span>

    <span class="espaciador"></span>

    <?php
    // Lo que se enseña: la simulación si se ha pedido una, y si no la
    // propuesta guardada.
    // Por defecto 8 columnas (6,00 EUR), no lo que se guardó la última vez:
    // si un día se probó con 2 columnas, mañana el panel no debe abrir con
    // 1,50 EUR como si fuera la apuesta normal.
    $vCols = $propuestaViva['columnas'] ?? ($recalcular ? $combinaciones : 8);
    $vDob  = $propuestaViva['dobles'] ?? $dobles;
    $vTri  = $propuestaViva['triples'] ?? 0;
    $vCoste = $vCols * $PRECIO_COLUMNA;
    $menos = ajustar_columnas($vCols, -1);
    $mas   = ajustar_columnas($vCols, +1);
    $base  = '?' . http_build_query(array_filter([
        'j' => ($jornada['etiqueta_temporada'] . '/' . (int)$jornada['numero_jornada']),
    ]));
    ?>
    <div class="presupuesto">
      <a class="paso" href="<?= e($base) ?>&cols=<?= $menos ?>"
         title="Bajar a <?= $menos ?> columnas">−</a>
      <span class="importe">
        <b><?= number_format($vCoste, 2, ',', '.') ?></b> €
        <i><?= $vCols ?> columna<?= $vCols === 1 ? '' : 's' ?><?php
          $partes = [];
          if ($vDob) { $partes[] = $vDob . ' doble' . ($vDob > 1 ? 's' : ''); }
          if ($vTri) { $partes[] = $vTri . ' triple' . ($vTri > 1 ? 's' : ''); }
          echo $partes ? ' · ' . implode(' + ', $partes) : '';
        ?></i>
      </span>
      <a class="paso" href="<?= e($base) ?>&cols=<?= $mas ?>"
         title="Subir a <?= $mas ?> columnas">+</a>
      <?php if (!$congelada): ?>
      <a class="calcular" href="<?= e($base) ?>&cols=<?= $vCols ?>&guardar=1"
         title="Recalcula la propuesta con este presupuesto y la guarda">Calcular</a>
      <?php endif; ?>
    </div>

    <?php
    // --- El análisis por IA -------------------------------------------
    //
    // El botón gasta dinero de verdad, así que lleva el precio escrito y
    // pide contraseña. Fuera de la ventana de 12 horas ni siquiera se
    // enseña: no hay nada que analizar a dos días vista.

    $avisos = [
        'lanzada'   => ['ok', 'Análisis lanzado. Tarda dos o tres minutos.'],
        'clave'     => ['mal', 'Contraseña incorrecta.'],
        'en_marcha' => ['aviso', 'Ya hay un análisis en marcha para esta jornada.'],
        'ya_hecho'  => ['aviso', 'Esta jornada ya se analizó. Uno por jornada.'],
        'pronto'    => ['aviso', $_GET['msg'] ?? 'Todavía es pronto.'],
        'tarde'     => ['mal', $_GET['msg'] ?? 'La jornada ya ha empezado.'],
        'error'     => ['mal', $_GET['msg'] ?? 'No se pudo lanzar.'],
    ];
    $iaMensaje = $avisos[$_GET['ia'] ?? ''] ?? null;

    $puedeLanzar = $horasAlCierre !== null
        && $horasAlCierre > 0
        && $horasAlCierre <= IA_VENTANA_HORAS
        && $iaEstado === null;
    ?>

    <div class="ia">
      <?php if ($horasAlCierre !== null && $horasAlCierre > 0): ?>
        <span class="cuenta" data-inicio="<?= (int)$primerPartido ?>">
          <b>—</b><i>para el primer partido</i>
        </span>
      <?php endif; ?>

      <?php if ($iaEstado === 'en_cola' || $iaEstado === 'analizando'): ?>
        <span class="ia-estado trabajando">analizando…</span>
        <?php /* La página se refresca sola hasta que el análisis aparece. */ ?>
        <meta http-equiv="refresh" content="20">

      <?php elseif ($iaEstado === 'hecho' && $iaAnalisis): ?>
        <span class="ia-estado listo" title="<?= e(sprintf(
            '%d ajustes · %s · %s',
            count($iaAjustes),
            number_format(((float)$iaAnalisis['coste_usd']) * 0.92, 2, ',', '.') . ' €',
            (string)$iaAnalisis['creado_en']
        )) ?>">
          analizado<?= $iaAjustes
              ? ' · ' . count($iaAjustes) . ' ajuste' . (count($iaAjustes) > 1 ? 's' : '')
              : ' · sin cambios' ?>
        </span>

      <?php elseif ($puedeLanzar): ?>
        <form class="ia-form" method="post" action="lanzar_ia.php"
              onsubmit="return confirm('Lanzar el análisis por IA?\n\nCuesta unos 1,90 € y solo se puede hacer una vez en esta jornada.')">
          <input type="hidden" name="jornada_id"
                 value="<?= (int)$jornada['jornada_id'] ?>">
          <input type="password" name="clave" placeholder="clave"
                 autocomplete="off" required>
          <button type="submit">Analizar con IA · 1,90 €</button>
        </form>
        <?php if ($horasAlCierre > 6): ?>
          <span class="ia-nota">a esta hora aún no hay alineaciones probables</span>
        <?php endif; ?>

      <?php elseif ($iaEstado === 'caducado'): ?>
        <span class="ia-estado" title="El workflow no llegó a guardar nada. Se puede volver a intentar.">
          el intento anterior no terminó
        </span>
        <form class="ia-form" method="post" action="lanzar_ia.php"
              onsubmit="return confirm('Reintentar el análisis?\n\nCuesta unos 1,90 €.')">
          <input type="hidden" name="jornada_id"
                 value="<?= (int)$jornada['jornada_id'] ?>">
          <input type="password" name="clave" placeholder="clave"
                 autocomplete="off" required>
          <button type="submit">Reintentar</button>
        </form>

      <?php elseif ($horasAlCierre !== null && $horasAlCierre > IA_VENTANA_HORAS): ?>
        <span class="ia-estado espera">
          la IA se abre a <?= (int)IA_VENTANA_HORAS ?> h del primer partido
        </span>
      <?php endif; ?>

      <?php if ($congelada): ?>
        <span class="ia-estado cerrado">boleto cerrado</span>
        <a class="ia-abrir" href="<?= e($base) ?>&descongelar=1"
           title="Vuelve a permitir cambiar el presupuesto. No llama a la IA: no cuesta nada.">abrir</a>
      <?php endif; ?>
    </div>

    <?php if ($iaMensaje): ?>
      <div class="ia-aviso <?= e($iaMensaje[0]) ?>"><?= e((string)$iaMensaje[1]) ?></div>
    <?php endif; ?>

    <?php
    // Un solo campo con temporada y jornada juntas: separarlas daría
    // combinaciones inexistentes al cambiar de temporada.
    ?>
    <form method="get" id="f">
      <select name="j" onchange="document.getElementById('f').submit()">
        <?php foreach ($jornadasDisponibles as $j):
            $v = $j['etiqueta_temporada'] . '/' . (int)$j['numero_jornada'];
            $actual = $jornada['etiqueta_temporada'] . '/' . (int)$jornada['numero_jornada'];
        ?>
          <?php
          // Una jornada resuelta enseña su combinación; una pendiente, nada.
          $comb = trim((string)($j['combinacion'] ?? ''));
          $resuelta = strlen($comb) === 14;
          ?>
          <option value="<?= e($v) ?>" <?= $v === $actual ? 'selected' : '' ?>>
            J<?= (int)$j['numero_jornada'] ?> · <?= e($j['etiqueta_temporada']) ?><?=
              $resuelta ? '  ·  ' . e($comb) : '  ·  pendiente' ?>
          </option>
        <?php endforeach; ?>
      </select>
    </form>
  </div>

  <div class="panel">
    <div class="scroll">
    <table>
      <thead>
        <tr>
          <th></th>
          <th></th>
          <th class="izq">Partido</th>
          <th><?= $jugada ? 'Resultado' : 'Propuesta' ?></th>
          <th class="grupo" title="Porcentaje de gente que juega cada signo, según loteriasyapuestas.es">% LAE</th>
          <th class="grupo" title="Probabilidad implícita en las cuotas: BetFair y Matchbook en LaLiga y Segunda, mediana de doce casas en Liga F">% Apuestas</th>
          <th class="grupo" title="Pronóstico de nuestro modelo Elo, entrenado con el histórico de la base. Solo aparece donde el modelo demuestra superar al baseline">% Predictor</th>
          <th title="Probabilidad de mercado dividida por lo que juega la gente. Por encima de 1, el signo paga más de lo que le corresponde">Valor</th>
        </tr>
      </thead>
      <tbody>
      <?php foreach ($casillas as $c):
          $pos = (int)$c['posicion'];
          if ($pos === 15) { continue; }

          $pub = terna($publico[$pos] ?? null, 'prob_1', 'prob_x', 'prob_2');
          $pid = $c['partido_id'] !== null ? (int)$c['partido_id'] : null;
          $mer = terna(
              $pid !== null ? ($mercado[$pid] ?? null) : null,
              'prob_mercado_local', 'prob_mercado_empate', 'prob_mercado_visitante'
          );
          $mod = terna($modelo[$pos] ?? null, 'prob_1', 'prob_x', 'prob_2');
          // La simulación manda sobre lo guardado mientras se está
          // ajustando el presupuesto.
          $marcados = $propuestaViva
              ? ($propuestaViva['selecciones'][$pos] ?? [])
              : ($propuesta['selecciones'][$pos] ?? []);
          [$dia, $hora] = horario($c['fecha_hora_inicio'] ?? null);

          // Con un doble se enseña el valor del mejor de los dos signos.
          $vMostrar = null;
          foreach ($marcados as $s) {
              $v = valor($mer, $pub, $s);
              if ($v !== null && ($vMostrar === null || $v > $vMostrar)) {
                  $vMostrar = $v;
              }
          }
      ?>
        <tr class="partido">
          <td class="pos"><?= $pos ?></td>
          <td class="hora">
            <?php if ($dia): ?>
              <span class="d"><?= e($dia) ?></span><span class="h"><?= e($hora) ?></span>
            <?php else: ?>
              <span class="h">·</span>
            <?php endif; ?>
          </td>
          <td class="equipos">
            <?= e($c['equipo_local_impreso']) ?><span class="g">–</span><?= e($c['equipo_visitante_impreso']) ?>
            <?php if (($c['genero'] ?? '') === 'FEMENINO'): ?><span class="f">F</span><?php endif; ?>
          </td>
          <td class="boleto">
            <div class="caja">
              <?php foreach ($SIGNOS as $s):
                  $on = $jugada
                      ? ($s === ($c['signo_oficial'] ?? null))
                      : in_array($s, $marcados, true);
                  // En una jornada jugada con propuesta guardada se señala
                  // además si el signo propuesto acertó o falló.
                  $extra = '';
                  if ($jugada && $marcados) {
                      if (in_array($s, $marcados, true)) {
                          $extra = $s === ($c['signo_oficial'] ?? null) ? ' acierto' : ' fallo';
                      }
                  }
              ?>
                <i class="<?= ($on ? 'on' : '') . $extra ?>"><?= $s ?></i>
              <?php endforeach; ?>
            </div>
          </td>
          <td class="pct">
            <div class="g">
              <?php foreach ($SIGNOS as $s): ?>
                <span class="<?= $pub ? '' : 'nulo' ?>"><?= $pub ? round($pub[$s] * 100) : '·' ?></span>
              <?php endforeach; ?>
            </div>
          </td>
          <?php
          // Un hueco mudo se lee como un fallo. Se dice por qué está vacío.
          // De dónde sale el dato: no se esconde que una casilla venga de
          // una sola casa y otra de un exchange. Cambia cuánto fiarse.
          $casaMer = $pid !== null ? ($mercado[$pid]['casa_apuestas'] ?? null) : null;
          $ORIGEN = [
              'betfair_ex_eu' => 'Betfair Exchange · 0,8% de margen',
              'matchbook' => 'Matchbook Exchange · 0,8% de margen',
              'sportytrader_mediana' => 'Mediana de doce casas (Bet365, Bwin, William Hill, Unibet, 1xBet…)',
          ];
          $porQueNoMer = $mer
              ? ($ORIGEN[$casaMer] ?? ('Fuente: ' . $casaMer))
              : 'Ninguna casa de las que consultamos cotiza este partido todavía.';
          $porQueNoPred = $mod ? '' : 'El modelo no supera al baseline en esta competición, así que no se publica su pronóstico.';
          ?>
          <td class="pct mer"<?= $porQueNoMer ? ' title="' . e($porQueNoMer) . '"' : '' ?>>
            <div class="g">
              <?php foreach ($SIGNOS as $s):
                  [$f, $cl] = flecha($mer, $pub, $s);
              ?>
                <span class="<?= $mer ? $cl : 'nulo' ?>"><?= $mer ? round($mer[$s] * 100) : '·'
                  ?><?= $f ? '<em>' . $f . '</em>' : '' ?></span>
              <?php endforeach; ?>
            </div>
          </td>
          <td class="pct pred"<?= $porQueNoPred ? ' title="' . e($porQueNoPred) . '"' : '' ?>>
            <div class="g">
              <?php foreach ($SIGNOS as $s): ?>
                <span class="<?= $mod ? '' : 'nulo' ?>"><?= $mod ? round($mod[$s] * 100) : '·' ?></span>
              <?php endforeach; ?>
            </div>
          </td>
          <td class="val <?= claseValor($vMostrar) ?>">
            <?= $vMostrar !== null ? number_format($vMostrar, 2, ',', '.') : '·' ?>
          </td>
        </tr>
      <?php endforeach; ?>

      <?php
      // El Pleno va dentro de la misma tabla, como en el boleto real: dos
      // filas, una por equipo, con las cuatro categorías de goles.
      $c15 = null;
      foreach ($casillas as $c) {
          if ((int)$c['posicion'] === 15) { $c15 = $c; }
      }
      if ($c15):
          $marcadosPleno = $propuesta['selecciones'][15] ?? [];
          [$d15, $h15] = horario($c15['fecha_hora_inicio'] ?? null);
          $lados = [
              'local' => $c15['equipo_local_impreso'],
              'visitante' => $c15['equipo_visitante_impreso'],
          ];
          $n = 0;
          foreach ($lados as $lado => $nombre):
              $n++;
              $fila = $pleno[$lado] ?? null;
      ?>
        <tr class="pleno pleno-<?= $n ?>">
          <td class="pos"><?= $n === 1 ? 15 : '' ?></td>
          <td class="hora">
            <?php if ($n === 1 && $d15): ?>
              <span class="d"><?= e($d15) ?></span><span class="h"><?= e($h15) ?></span>
            <?php endif; ?>
          </td>
          <td class="equipos"><?= e($nombre) ?></td>
          <td class="boleto p15">
            <div class="caja">
              <?php foreach ($CATEGORIAS as $cat):
                  if ($jugada) {
                      $esperado = pleno_lado($plenoOficial, $lado);
                      $on = $esperado !== null && $cat === $esperado;
                  } else {
                      // La propuesta solo guarda el lado local: el endpoint
                      // acepta una lista de signos por casilla y el Pleno
                      // necesita dos.
                      // Los dos lados se marcan igual: con la categoría más
                      // probable de su propia fuente. Antes el local salía
                      // en blanco porque se leía de la propuesta guardada,
                      // que no incluye la casilla 15.
                      $on = ($plenoPropuesta[$lado]['categoria'] ?? null) === $cat;
                  }
              ?>
                <i class="<?= $on ? 'on' : '' ?>"><?= $cat ?></i>
              <?php endforeach; ?>
            </div>
          </td>
          <?php $filaMer = $plenoMercado[$lado] ?? null; ?>
          <td class="pct p15">
            <div class="g">
              <?php foreach ($CATEGORIAS as $cat):
                  $k = ['0' => 'prob_0', '1' => 'prob_1', '2' => 'prob_2', 'M' => 'prob_m'][$cat];
              ?>
                <span class="<?= $fila ? '' : 'nulo' ?>"><?= $fila ? round((float)$fila[$k] * 100) : '·' ?></span>
              <?php endforeach; ?>
            </div>
          </td>
          <td class="pct mer p15">
            <div class="g">
              <?php foreach ($CATEGORIAS as $cat):
                  $k = ['0' => 'prob_0', '1' => 'prob_1', '2' => 'prob_2', 'M' => 'prob_m'][$cat];
              ?>
                <span class="<?= $filaMer ? '' : 'nulo' ?>"><?= $filaMer ? round((float)$filaMer[$k] * 100) : '·' ?></span>
              <?php endforeach; ?>
            </div>
          </td>
          <?php $filaMod = $plenoModelo[$lado] ?? null; ?>
          <td class="pct pred p15">
            <div class="g">
              <?php foreach ($CATEGORIAS as $cat):
                  $k = ['0' => 'prob_0', '1' => 'prob_1', '2' => 'prob_2', 'M' => 'prob_m'][$cat];
              ?>
                <span class="<?= $filaMod ? '' : 'nulo' ?>"><?= $filaMod ? round((float)$filaMod[$k] * 100) : '·' ?></span>
              <?php endforeach; ?>
            </div>
          </td>
          <td class="val">·</td>
        </tr>
      <?php endforeach; endif; ?>
      </tbody>
    </table>
    </div>

    <?php if ($conMercado < count($casillas) - 1): ?>
      <?php
      $sinCuotas = count($casillas) - 1 - $conMercado;
      // El pronóstico está en $modelo, indexado por posición. Buscarlo en
      // $casillas no funciona: esas filas vienen de la consulta a
      // quiniela_partidos_jornada y no traen las probabilidades.
      $sinPred = 0;
      foreach ($casillas as $c) {
          $pos = (int)$c['posicion'];
          if ($pos === 15) { continue; }
          if (empty($modelo[$pos])) { $sinPred++; }
      }
      ?>
      <div class="aviso">
        <span>
        <?php if ($jugada): ?>
          <b>Jornada del histórico.</b> Trae resultado y porcentajes, pero no
          cuotas ni pronóstico: ambos se calculan en directo, antes de jugarse.
        <?php else: ?>
          <b>Los huecos no son errores.</b>
          <?php if ($sinCuotas): ?>
            En <?= $sinCuotas ?> no hay <b>% Apuestas</b>: el comparador aún no
            ha publicado cuotas de esos partidos.
          <?php endif; ?>
          <?php if ($sinPred): ?>
            En <?= $sinPred ?> no hay <b>Predictor</b>: el modelo se mide por
            competición antes de publicarse y ahí no supera al baseline, así
            que se descarta solo.
          <?php endif; ?>
          <?php if ($sinCuotas && $sinPred): ?>
            Se complementan: toda casilla tiene al menos una de las dos señales.
          <?php elseif ($sinCuotas): ?>
            El <b>Predictor</b> las cubre: es la única señal que hay ahí.
          <?php endif; ?>
        <?php endif; ?>
        </span>
      </div>
    <?php endif; ?>
  </div>

  <footer>
    <div class="leyenda">
      <span><i class="mini">1</i> propuesta</span>
      <span style="color:var(--sube)">▲ el mercado le da más que el público (infrajugado)</span>
      <span style="color:var(--baja)">▼ menos (sobrejugado)</span>
    </div>
    <p>Las tres columnas de cifras dicen cosas distintas.
    <b>% LAE</b> es cuánta gente juega cada signo en la quiniela
    (loteriasyapuestas.es): dice qué hace el público, no qué va a pasar.
    <b>% Apuestas</b> es la probabilidad implícita en las cuotas. En LaLiga y
    Segunda sale de BetFair y Matchbook, mercados de intercambio que cotizan
    con un 0,8% de margen frente al 4,7% de las casas al uso; en Liga F, de
    1xBet, la única casa que publica la jornada completa.
    <b>% Predictor</b> es nuestro modelo Elo, entrenado con los 4.400 partidos
    de la base.</p>
    <p>El Predictor combina valoración Elo, forma de las últimas cinco
    jornadas, goles recientes, días de descanso y enfrentamientos directos.
    Cada partido se describe solo con lo anterior a él. Se mide por
    competición antes de publicarse y se descarta donde no supere al
    baseline: en <b>Liga F acierta el 62,5%</b> y es la única señal que hay
    porque nadie cotiza esos partidos; en <b>Segunda, el 47,1%</b>, la más
    difícil por ser la más igualada.</p>
    <p><b>Valor</b> = Mercado ÷ LAE. Por encima de 1 el signo está infrajugado y
    paga más de lo que le corresponde, porque el premio se reparte entre
    acertantes. El 2 del Sporting vale 1,40: lo juega el 28% y el mercado le da
    un 39%.</p>
    <p>Backtest sobre 989 jornadas con premios reales: <b>ninguna estrategia dio
    beneficio</b>. La mejor perdió 14,4% (IC 90%: −40% a +17%) frente al −44,8%
    de jugar al favorito y el −69,2% de seguir al público. Esto reduce la pérdida
    esperada; no la convierte en ganancia.</p>
  </footer>

<?php endif; ?>

</div>
<script>
/* El contador hasta el primer partido.
   La hora viene del servidor en segundos desde época, así que no depende de
   que el reloj del navegador esté en la misma zona horaria. */
(function () {
  var caja = document.querySelector('.ia .cuenta');
  if (!caja) { return; }
  var inicio = parseInt(caja.dataset.inicio, 10) * 1000;
  var salida = caja.querySelector('b');

  function pinta() {
    var falta = inicio - Date.now();
    if (falta <= 0) {
      salida.textContent = 'en juego';
      caja.querySelector('i').textContent = 'la jornada ha empezado';
      return;
    }
    var h = Math.floor(falta / 3600000);
    var m = Math.floor(falta / 60000) % 60;
    salida.textContent = h >= 1 ? h + ' h ' + m + ' min' : m + ' min';
  }

  pinta();
  setInterval(pinta, 30000);
})();
</script>
</body>
</html>
