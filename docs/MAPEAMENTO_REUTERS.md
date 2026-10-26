# Mapeamento editorial da Reuters — 26/10/2026

Rodada finita iniciada pelo endereço `https://www.reuters.com/sitemap/`.
O mapa HTML retornou HTTP 401 com tela de proteção. As declarações públicas de sitemaps
do próprio site permitiram localizar e conferir outros caminhos oficiais.
Não houve chamadas ao Bright Data, OpenRouter, criação de rodada de notícias ou busca ilimitada.

## Resultado

| Caminho | Resultado observado | Utilização |
| --- | --- | --- |
| `/sitemap/` | HTTP 401 | Mapa HTML inacessível pelo HTTP atual |
| `/arc/outboundfeeds/sitemap-index/?outputType=xml` | HTTP 401 | Candidato a índice editorial; funcionalidade não comprovada |
| `/arc/outboundfeeds/news-sitemap-index/?outputType=xml` | HTTP 401 | Candidato a atualizações; funcionalidade não comprovada |
| Índices japonês, árabe e Practical Law Journal | HTTP 401 | Não ativados |
| `/service/archive-sitemap/sitemap.xml` | HTTP 200, índice com 6.391 filhos de 2005 a outubro de 2023 | XML acessível; seis reportagens amostradas retornaram 401, sem texto confirmado |
| `/graphics/sitemap.xml` | HTTP 200, 493 URLs distintas; três amostras textuais confirmadas | Cadastrado como arquivo editorial funcional, fora da busca diária |
| `/graphics/` | Página HTML acessível, sem RSS alternativo identificado | Não é um link de coleta RSS/sitemap |
| Alternativas `graphics/rss.xml` e `graphics/sitemap-index.xml` | HTTP 403 | Não cadastradas como funcionais |
| Reuters Plus (controle) | XML acessível | Conteúdo de marca; não comprova cobertura editorial geral |

## Link cadastrado

Fonte Reuters, ID 377. Link ID **40548**:
`https://www.reuters.com/graphics/sitemap.xml`, mecanismo `sitemap`, ativo no cadastro,
com teste e três exemplos preservados. A finalidade em `ingest_daily_routes` é `archive`:
a seleção diária padrão o exclui, evitando baixar centenas de artigos históricos em cada rodada.
A periodicidade cadastrada é de 240 minutos; a estimativa amostral original continua no teste.

As datas de publicação declaradas no XML examinado vão de setembro de 2019 a 16/10/2024.
As amostras entregaram reportagens/texto de 2021 e 2024, incluindo:

- How Israel’s bulky pager fooled Hezbollah — 10.789 caracteres extraídos.
- Carbon recyclers — 26.348 caracteres extraídos.
- Ukraine UEFA Euro 2024 squad — 588 caracteres extraídos.

As contagens de caracteres são do extrator no teste, não uma garantia de integralidade editorial.
Acessibilidade histórica não comprova atualização do sitemap hoje nem cobertura de todas as seções.
Nenhum novo caminho foi comprovado para o noticiário diário da Reuters nesta rodada.

## Evidências e limites

Artefatos em `var/gerenciador/reuters-editorial-20261026/`:

- `links.csv`: endereços, respostas, formatos e comprovação textual.
- `graphics-test.json`: resultado do mecanismo real do gerenciador.
- `registration.json`: vínculo cadastrado e finalidade histórica.
- `auditoria-final.json`: resumo, respostas, hashes e contagem de requisições.
- `*.body.gz`: cópias das respostas examinadas.

A exploração leu 37 documentos/páginas e testou seis reportagens históricas, mais as três
amostras de Graphics. Total: 47 requisições HTTP. As recusas permanentes 401/403/404 não
foram repetidas indefinidamente. Não foram cadastrados milhares de XMLs filhos nem
ativados endereços sem comprovação textual. Os sete links anteriores do Reuters Plus e
o histórico do catálogo foram preservados.
