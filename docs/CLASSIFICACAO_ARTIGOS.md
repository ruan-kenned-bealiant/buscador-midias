# Classificação e origem editorial de artigos

O prompt `context-2026-10-26.1` acrescenta dois dados à análise geral, sem alterar o estado
operacional da tarefa nem tornar notícia sem fato adverso uma notícia adversa.

## Categoria do conteúdo

`content_category` usa `news`, `opinion`, `weather`, `cooking`, `sports`, `entertainment`,
`lifestyle`, `horoscope` ou `null`. A natureza opinativa tem precedência sobre o assunto;
blogs e colunas não são opinativos só por sua seção ou URL. Receitas e previsão do tempo
rotineira têm categorias específicas; investigações em restaurantes e reportagens sobre
responsabilidade por desastres não são confundidas com receitas ou boletins meteorológicos.

A regra de opinião segue o commit `7d47ff5d2` da branch
`feature/31367-melhorias-midias-negativas` do KyX. As categorias temáticas adicionais são
uma extensão do buscador. A classificação de risco, as entidades e os temas continuam
independentes. Valor desconhecido não vira `news`: permanece nulo.

Novos registros gravam a categoria. Em regravação, uma categoria existente é preservada;
preenche-se apenas a que estiver nula, inclusive para registros de outra origem, como no KyX.
Isso não provoca uma reanálise automática de todos os artigos já completos no corpus.
Consumidores que usam o enum PHP antigo (apenas `news` e `opinion`) precisam ampliar seu
vocabulário para interpretar as novas categorias; o buscador não altera os outros projetos.

## País: idioma → artigo → fonte

É o país da origem editorial, não o local do fato nem a nacionalidade da pessoa citada.
Segue a precedência de `OutletCountryResolver` do PowerDD:

1. Região válida do idioma extraído do HTML, como `pt-BR` ou `pt-PT`.
2. País válido salvo/identificado no artigo, quando há comprovação editorial.
3. País do cadastro ativo de fontes, com host mais específico e caminho mais longo.
4. Sem evidência válida, `NULL` (a interface consumidora pode exibir “Internacional”).

`pt` e `en` não determinam países. `es-419` não é um país. `en-US` não decide por idioma:
segue a exceção já adotada no PowerDD para padrões de CMS. `US` continua sendo um país válido
nas outras origens. Códigos são validados como ISO alfa-2. Fontes compostas, como `BR/GB`,
preservam ambos os países quando o cadastro é a origem da decisão.

A IA retorna `article_country` somente com `article_country_evidence`, citação literal curta
sobre edição/origem editorial, validada contra o texto recebido. Não deve usar o país do
assunto nem inventar a origem pelo domínio. A precedência é resolvida em Python;
a resposta do modelo não a substitui. O corpus grava `source_country` e
`source_country_method` (`language`, `article`, `site` ou `NULL`).

O índice do cadastro de países é compartilhado entre os workers de gravação, atualizado
após cinco minutos e reutilizado entre artigos. Não há nova chamada à IA ou HTTP para
resolver o país.

## Persistência e implantação

As filas Rabbit preservam os campos no JSON da análise. O writer grava os campos no corpus;
a esteira legada de enrich/sync também os conserva com a migration
`sql/postgres/008_article_classification.sql`. As colunas do corpus já existentes podem
ser preparadas de forma idempotente por `sql/corpus/001_article_classification.sql`.

Uma análise antiga continua identificada por sua versão original, com campos ausentes
tratados como desconhecidos. A implantação não reinicia rodadas nem reclassifica em massa
artigos completos: análises executadas após a recarga usam a nova versão.
