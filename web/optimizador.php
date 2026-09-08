<?php
declare(strict_types=1);

/**
 * El optimizador de boletos, en PHP, para poder recalcular al vuelo.
 *
 * Por qué existe una segunda copia
 * --------------------------------
 *
 * La versión que manda sigue siendo optimizador/cartera.py: es la que corre
 * en el workflow y la que deja la propuesta guardada en la base. Esta es un
 * puerto fiel para que el panel pueda recalcular cuando se cambia el
 * presupuesto, sin ir y volver a GitHub Actions.
 *
 * Las dos tienen que dar el mismo resultado con los mismos datos. Si se
 * toca una, hay que tocar la otra.
 *
 * Cómo se juega una múltiple
 * --------------------------
 *
 * Cada doble multiplica por dos las columnas y cada triple por tres, así
 * que el número de columnas de un boleto siempre es 2^dobles x 3^triples.
 * Eso deja fuera la mayoría de los números: se puede jugar 8 columnas o 9,
 * pero no 10 ni 11. Por eso el presupuesto se mueve por una escalera de
 * valores válidos y no de euro en euro.
 *
 * A 0,75 EUR la columna:
 *
 *     1 col    0,75      apuesta simple
 *     2        1,50      1 doble
 *     3        2,25      1 triple
 *     4        3,00      2 dobles
 *     6        4,50      1 doble + 1 triple
 *     8        6,00      3 dobles
 *     9        6,75      2 triples
 *    12        9,00      2 dobles + 1 triple
 *     ...
 */

const SIGNOS = ['1', 'X', '2'];
const PRECIO_COLUMNA = 0.75;

// Hasta dónde se deja subir el presupuesto en el panel. 1.296 columnas son
// 972 EUR: más que suficiente, y evita que un clic accidental proponga un
// boleto de miles de euros.
const COLUMNAS_MAXIMAS = 1296;


/**
 * Los números de columnas que se pueden jugar de verdad, en orden.
 *
 * Cada uno con la descomposición que abre menos casillas: para 12 columnas
 * se prefiere 2 dobles y 1 triple (3 casillas abiertas) antes que otras
 * combinaciones equivalentes, porque cuantas menos casillas se abran, más
 * concentrado queda el boleto en las que de verdad están dudosas.
 */
function escalera_apuestas(): array
{
    static $cache = null;
    if ($cache !== null) {
        return $cache;
    }

    $combos = [];
    for ($d = 0; $d <= 14; $d++) {
        for ($t = 0; $t + $d <= 14; $t++) {
            $n = (2 ** $d) * (3 ** $t);
            if ($n > COLUMNAS_MAXIMAS) {
                continue;
            }
            if (!isset($combos[$n]) || ($d + $t) < array_sum($combos[$n])) {
                $combos[$n] = [$d, $t];
            }
        }
    }
    ksort($combos);
    $cache = $combos;
    return $cache;
}


/** El valor más cercano de la escalera, hacia arriba o hacia abajo. */
function ajustar_columnas(int $pedidas, int $direccion = 0): int
{
    $valores = array_keys(escalera_apuestas());

    if ($direccion === 0) {
        // El más cercano que no pase del pedido.
        $mejor = $valores[0];
        foreach ($valores as $v) {
            if ($v <= $pedidas) {
                $mejor = $v;
            }
        }
        return $mejor;
    }

    foreach (($direccion > 0 ? $valores : array_reverse($valores)) as $v) {
        if ($direccion > 0 && $v > $pedidas) {
            return $v;
        }
        if ($direccion < 0 && $v < $pedidas) {
            return $v;
        }
    }
    return $direccion > 0 ? end($valores) : $valores[0];
}


/**
 * Valor de un signo: probabilidad dividida por cuánta gente lo juega.
 *
 * Por encima de 1 está infrajugado y paga más de lo que le corresponde,
 * porque el premio se reparte entre acertantes.
 */
function valor_signo(?array $prob, ?array $pub, string $s): ?float
{
    if (!$prob || !$pub) {
        return null;
    }
    return $prob[$s] / max($pub[$s], 0.005);
}


/** Incertidumbre en base 3: 1 es un partido totalmente abierto. */
// Qué fracción de $probabilidad_minima debe alcanzar el segundo signo para
// que merezca la pena abrir un doble. Atado a ese parámetro, que salió de
// las 989 jornadas del histórico, en vez de ser un número suelto.
const FRACCION_SEGUNDO = 0.75;


function entropia(array $terna): float
{
    $h = 0.0;
    foreach (SIGNOS as $s) {
        $p = max($terna[$s], 1e-9);
        $h -= $p * (log($p) / log(3));
    }
    return $h;
}


/**
 * Cuánto merece la pena abrir esta casilla.
 *
 * Combina dos cosas: cuán incierto está el partido y cuánto se desvía el
 * público del mercado. Una casilla donde el segundo signo está infrajugado
 * es mejor candidata que una simplemente igualada, porque ahí el doble no
 * solo cubre riesgo: además compra valor.
 *
 * Sin probabilidad propia solo queda la incertidumbre del público, y se
 * penaliza: abrir a ciegas cuesta lo mismo que abrir con criterio y rinde
 * menos.
 */
function prioridad_apertura(array $c, float $probabilidad_minima = 0.30): float
{
    $fuente = $c['prob'] ?? $c['pub'];
    if (!$fuente) {
        return 0.0;
    }
    $h = entropia($fuente);

    if (!$c['prob']) {
        return $h * 0.6;
    }

    $orden = SIGNOS;
    usort($orden, static fn($a, $b) => $c['prob'][$b] <=> $c['prob'][$a]);

    // Un doble compra la probabilidad del segundo signo a cambio de
    // duplicar las columnas. Si ese segundo signo es improbable, es dinero
    // tirado por mucho valor que tenga.
    //
    // Sin esta puerta, el multiplicador de valor arrastraba a abrir
    // partidos decididos: en la jornada 4 de 2026-27 los TRES dobles del
    // boleto se fueron a Liga F, que es la competición con los resultados
    // más claros, porque allí el público se concentra mucho y el segundo
    // signo tiene un porcentaje diminuto que dispara el valor.
    //
    // PUERTO de _prioridad_apertura() en optimizador/cartera.py.
    if ($c['prob'][$orden[1]] < $probabilidad_minima * FRACCION_SEGUNDO) {
        return 0.0;
    }

    $v = valor_signo($c['prob'], $c['pub'], $orden[1]) ?? 0.0;
    return $h * min($v, 2.5);
}


/** El signo más probable, según lo mejor que se tenga. */
function favorito(array $c): string
{
    $fuente = $c['prob'] ?? $c['pub'];
    $mejor = '1';
    foreach (SIGNOS as $s) {
        if ($fuente[$s] > $fuente[$mejor]) {
            $mejor = $s;
        }
    }
    return $mejor;
}


/**
 * Los signos de una casilla que se abre.
 *
 * El favorito entra siempre: es la cobertura. Los acompañantes se eligen
 * por VALOR y no por probabilidad, que es la diferencia que hace útil todo
 * esto. En un Rayo-Racing con mercado 43/29/28 y público 56/26/18, la X y
 * el 2 empatan en probabilidad, pero el 2 vale 1,56 y la X 1,10.
 */
function elegir_abierta(array $c, int $cuantos, float $probabilidad_minima): array
{
    $fav = favorito($c);
    $marcados = [$fav];

    if (!$c['prob']) {
        // Sin criterio propio, se acompaña por probabilidad del público.
        $resto = array_values(array_diff(SIGNOS, [$fav]));
        usort($resto, static fn($a, $b) => $c['pub'][$b] <=> $c['pub'][$a]);
        return array_merge($marcados, array_slice($resto, 0, $cuantos - 1));
    }

    $candidatos = [];
    foreach (SIGNOS as $s) {
        if ($s === $fav) {
            continue;
        }
        if ($c['prob'][$s] < $probabilidad_minima) {
            continue;
        }
        $candidatos[$s] = valor_signo($c['prob'], $c['pub'], $s) ?? 0.0;
    }
    arsort($candidatos);

    foreach (array_keys($candidatos) as $s) {
        if (count($marcados) >= $cuantos) {
            break;
        }
        $marcados[] = $s;
    }

    // Si el umbral dejó sin candidatos, se completa por probabilidad: una
    // casilla abierta con un solo signo sería un doble que no lo es.
    if (count($marcados) < $cuantos) {
        $resto = array_values(array_diff(SIGNOS, $marcados));
        usort($resto, static fn($a, $b) => $c['prob'][$b] <=> $c['prob'][$a]);
        foreach ($resto as $s) {
            if (count($marcados) >= $cuantos) {
                break;
            }
            $marcados[] = $s;
        }
    }

    return $marcados;
}


/** Signo único: el de mejor valor entre los suficientemente probables. */
function elegir_simple(array $c, float $probabilidad_minima): string
{
    if (!$c['prob']) {
        return favorito($c);
    }

    $candidatos = [];
    foreach (SIGNOS as $s) {
        if ($c['prob'][$s] >= $probabilidad_minima) {
            $candidatos[$s] = valor_signo($c['prob'], $c['pub'], $s) ?? 0.0;
        }
    }
    if (!$candidatos) {
        return favorito($c);
    }
    arsort($candidatos);
    return (string)array_key_first($candidatos);
}


/**
 * Reparte dobles y triples hasta agotar el presupuesto.
 *
 * $casillas es un array por posición con 'pub' y 'prob' (ternas 1/X/2 que
 * suman 1, o null). Devuelve los signos marcados por posición.
 */
/**
 * Si la casilla es de Liga F, mirando competición y la marca '(F)'.
 */
function es_liga_f(?string $competicion, string $local, string $visitante): bool
{
    $c = mb_strtolower((string)$competicion);
    if (str_contains($c, 'liga f') || str_contains($c, 'femenin')) {
        return true;
    }
    return str_contains(mb_strtolower($local . ' ' . $visitante), '(f)');
}


function construir_cartera(
    array $casillas,
    int $columnas,
    float $probabilidad_minima = 0.30
): array {
    $escalera = escalera_apuestas();
    $columnas = ajustar_columnas($columnas);
    [$nDobles, $nTriples] = $escalera[$columnas] ?? [0, 0];

    // Las casillas más dudosas y con más valor en el segundo signo se abren
    // primero, y los triples van a las de arriba del todo.
    $orden = array_keys($casillas);
    usort($orden, static function ($a, $b) use ($casillas) {
        return prioridad_apertura($casillas[$b], $probabilidad_minima)
            <=> prioridad_apertura($casillas[$a], $probabilidad_minima);
    });

    $abiertas = [];
    $i = 0;
    for ($n = 0; $n < $nTriples && $i < count($orden); $n++, $i++) {
        $abiertas[$orden[$i]] = 3;
    }
    for ($n = 0; $n < $nDobles && $i < count($orden); $n++, $i++) {
        $abiertas[$orden[$i]] = 2;
    }

    $salida = [];
    $reales = 1;
    foreach ($casillas as $pos => $c) {
        if (isset($abiertas[$pos])) {
            $marcados = elegir_abierta($c, $abiertas[$pos], $probabilidad_minima);
            $reales *= count($marcados);
        } else {
            $marcados = [elegir_simple($c, $probabilidad_minima)];
        }
        // En el orden del boleto, no en el de elección.
        $ordenados = [];
        foreach (SIGNOS as $s) {
            if (in_array($s, $marcados, true)) {
                $ordenados[] = $s;
            }
        }
        $salida[$pos] = $ordenados;
    }

    return [
        'selecciones' => $salida,
        'columnas' => $reales,
        'dobles' => $nDobles,
        'triples' => $nTriples,
        'coste' => $reales * PRECIO_COLUMNA,
    ];
}
