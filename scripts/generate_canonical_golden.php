<?php

/**
 * Gera o golden file de canonicalização de URL a partir do código PHP REAL do KyX.
 *
 * Uso:
 *   php scripts/generate_canonical_golden.php \
 *       <KyX-NovoDD-backend>/app/Services/MidiasIa/MidiasIaUrlCanonicalizer.php \
 *       tests/fixtures/canonical_urls.txt > tests/fixtures/canonical_golden.json
 *
 * O Python (buscador_midias.domain.url) precisa produzir exatamente a mesma saída: o corpus
 * existente foi hasheado por essa regra, e qualquer divergência orfanaria linhas antigas.
 */

require $argv[1];

use App\Services\MidiasIa\MidiasIaUrlCanonicalizer;

$canonicalizer = new MidiasIaUrlCanonicalizer();
$cases = [];

foreach (file($argv[2], FILE_IGNORE_NEW_LINES) as $line) {
    if ($line === '') {
        continue;
    }

    $cases[] = [
        'url' => $line,
        'canonical' => $canonicalizer->canonicalize($line),
        'hash' => $canonicalizer->hash($line),
        'domain' => $canonicalizer->domain($line),
    ];
}

echo json_encode(
    ['php_version' => PHP_VERSION, 'cases' => $cases],
    JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT
), "\n";
