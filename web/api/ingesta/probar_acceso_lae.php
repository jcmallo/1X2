<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

/**
 * Diagnóstico: ¿puede este servidor leer los servicios de loteriasyapuestas.es?
 *
 * LAE está detrás de Akamai y rechaza con 403 las peticiones desde fuera de
 * España o desde IPs de centros de datos conocidos. Desde una IP española de
 * hosting el resultado puede ser distinto, y de eso depende si el boleto de
 * cada jornada se puede cargar solo o hay que meterlo a mano.
 *
 * Este endpoint solo lee. No escribe nada en la base de datos.
 * Una vez tomada la decisión, se puede borrar.
 */

$objetivos = [
    'buscador_quiniela' => 'https://www.loteriasyapuestas.es/servicios/buscadorSorteos'
        . '?game_id=LAQU&celebrados=true&fechaInicioInclusiva=' . date('Ymd', strtotime('-30 days'))
        . '&fechaFinInclusiva=' . date('Ymd'),
    'fechas_sorteos' => 'https://www.loteriasyapuestas.es/servicios/fechasSorteosArchivos'
        . '?game_id=LAQU&celebrados=true&fechaInicioInclusiva=' . date('Ymd', strtotime('-30 days'))
        . '&fechaFinInclusiva=' . date('Ymd'),
    'pagina_resultados' => 'https://www.loteriasyapuestas.es/es/resultados/quiniela',
];

if (!function_exists('curl_init')) {
    api_fail('cURL no está disponible en este PHP.', 500);
}

$resultados = [];

foreach ($objetivos as $nombre => $url) {
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_TIMEOUT => 20,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_ENCODING => '',
        CURLOPT_HTTPHEADER => [
            'Accept: application/json, text/html;q=0.9,*/*;q=0.8',
            'Accept-Language: es-ES,es;q=0.9',
            'Cache-Control: no-cache',
        ],
        CURLOPT_USERAGENT =>
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            . '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]);

    $cuerpo = curl_exec($ch);
    $codigo = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    $error = curl_error($ch);
    curl_close($ch);

    $esJson = false;
    $muestra = null;

    if (is_string($cuerpo) && $cuerpo !== '') {
        $decodificado = json_decode($cuerpo, true);
        $esJson = is_array($decodificado);
        $muestra = substr(preg_replace('/\s+/', ' ', strip_tags($cuerpo)) ?? '', 0, 300);
    }

    $resultados[$nombre] = [
        'http' => $codigo,
        'bytes' => is_string($cuerpo) ? strlen($cuerpo) : 0,
        'es_json' => $esJson,
        'error_curl' => $error !== '' ? $error : null,
        'accesible' => $codigo === 200 && is_string($cuerpo) && $cuerpo !== '',
        'muestra' => $muestra,
    ];
}

$alguno = false;
foreach ($resultados as $r) {
    if ($r['accesible']) {
        $alguno = true;
        break;
    }
}

api_json([
    'ok' => true,
    'servidor_ve_lae' => $alguno,
    'veredicto' => $alguno
        ? 'IONOS sí puede leer LAE: el boleto se puede cargar automáticamente.'
        : 'IONOS tampoco puede leer LAE: habrá que introducir el boleto por otra vía.',
    'resultados' => $resultados,
]);
