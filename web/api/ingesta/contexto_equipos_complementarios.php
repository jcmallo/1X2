<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';

require_method('GET');
require_auth();

$pdo = db();

$temporada = isset($_GET['temporada'])
    ? trim((string)$_GET['temporada'])
    : '';

if ($temporada === '' || strlen($temporada) > 20) {
    api_fail('temporada inválida.', 422);
}

/*
 * El universo se deriva de los partidos YA EXISTENTES en LaLiga/Segunda.
 * No se consulta ni se crea ningún equipo externo.
 */
$sql = "
    SELECT
        e.equipo_id,
        e.nombre_canonico,
        x.id_equipo_fuente AS transfermarkt_id
    FROM nucleo_equipos e
    INNER JOIN (
        SELECT p.equipo_local_id AS equipo_id
        FROM nucleo_partidos p
        INNER JOIN nucleo_temporadas_competicion tc
            ON tc.temporada_competicion_id = p.temporada_competicion_id
        INNER JOIN nucleo_competiciones c
            ON c.competicion_id = tc.competicion_id
        INNER JOIN nucleo_temporadas t
            ON t.temporada_id = tc.temporada_id
        WHERE t.etiqueta = :temporada1
          AND c.genero = 'MASCULINO'
          AND c.nombre IN ('LaLiga','Segunda División')

        UNION

        SELECT p.equipo_visitante_id AS equipo_id
        FROM nucleo_partidos p
        INNER JOIN nucleo_temporadas_competicion tc
            ON tc.temporada_competicion_id = p.temporada_competicion_id
        INNER JOIN nucleo_competiciones c
            ON c.competicion_id = tc.competicion_id
        INNER JOIN nucleo_temporadas t
            ON t.temporada_id = tc.temporada_id
        WHERE t.etiqueta = :temporada2
          AND c.genero = 'MASCULINO'
          AND c.nombre IN ('LaLiga','Segunda División')
    ) u
        ON u.equipo_id = e.equipo_id
    LEFT JOIN nucleo_ids_externos_equipo x
        ON x.equipo_id = e.equipo_id
       AND x.fuente = 'transfermarkt.com'
    WHERE e.genero = 'MASCULINO'
    ORDER BY e.nombre_canonico
";

$stmt = $pdo->prepare($sql);
$stmt->execute([
    ':temporada1' => $temporada,
    ':temporada2' => $temporada,
]);

$items = [];
foreach ($stmt->fetchAll() as $row) {
    $items[] = [
        'equipo_id' => (int)$row['equipo_id'],
        'nombre_canonico' => (string)$row['nombre_canonico'],
        'transfermarkt_id' => (
            $row['transfermarkt_id'] !== null
            ? (string)$row['transfermarkt_id']
            : null
        ),
    ];
}

api_json([
    'ok' => true,
    'temporada' => $temporada,
    'items' => $items,
    'total' => count($items),
]);
