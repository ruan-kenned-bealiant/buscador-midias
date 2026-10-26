# Catálogo, mecanismos e saúde das fontes

## Site, link, mecanismo e transporte

Um **site** representa a fonte editorial, com nome, URL, país, idioma, observações e estado. Cada **link** pertence a um site e guarda URL RSS/sitemap, rótulo, estado, mecanismo, transporte e intervalo de busca. O estado efetivo exige pai e filho ativos e não excluídos. Inativar o pai não apaga a escolha individual de cada filho.

Excluir é lógico (`deleted_at`), com auditoria. A interface não oferece uma restauração geral de todo registro excluído; as recuperações automáticas são específicas e verificam a proveniência da exclusão. Não se deve simplesmente reativar tudo por SQL.

As configurações dos mecanismos são globais por modelo, com `version`. O link guarda a seleção, e o teste registra os parâmetros/revisões observados. Alterar um modelo afeta testes e rodadas futuras; snapshots antigos permanecem. Alterar URL/tipo/configuração pode invalidar a prova atual. Cadastro novo não ganha comprovação só por ter sido salvo.

O intervalo é persistido em `search_interval_seconds`, editável na interface em unidades legíveis, inclusive minutos. Sem definição, o padrão é **240 minutos**. A redução de intervalos maiores que quatro horas foi um ajuste dos dados existentes, não um teto geral de validação. A cadência estimada de publicação é um campo separado; não é a agenda garantida do link. No modo de busca por horários fixos, o intervalo individual não determina a execução.

## Os seis mecanismos

Registro: [mechanisms/__init__.py](../../src/buscador_midias/catalog/mechanisms/__init__.py). Contrato: [base.py](../../src/buscador_midias/catalog/mechanisms/base.py).

| ID | Arquivo | Aceita / comportamento | Exemplo de escolha |
|---|---|---|---|
| `auto` | [automatic.py](../../src/buscador_midias/catalog/mechanisms/automatic.py) | Reconhece feed ou sitemap; segue filhos de índices e ajusta o tipo detectado | XML de formato ainda desconhecido |
| `rss` | [rss.py](../../src/buscador_midias/catalog/mechanisms/rss.py) | RSS, Atom ou RDF; não expande índices de sitemap | Feed de política com entradas e links de artigos |
| `sitemap` | [sitemap.py](../../src/buscador_midias/catalog/mechanisms/sitemap.py) | `urlset` direto; rejeita índice como lista de notícias | XML com `<url><loc>…</loc></url>` |
| `sitemap_tree` | [sitemap_index.py](../../src/buscador_midias/catalog/mechanisms/sitemap_index.py) | Índices e folhas; expõe filhos ao executor | Índice que aponta para XMLs mensais/diários |
| `news_sitemap` | [news_sitemap.py](../../src/buscador_midias/catalog/mechanisms/news_sitemap.py) | `urlset` com metadados Google News | Sitemap com publicação explícita por notícia |
| `g1_daily` | [g1_daily.py](../../src/buscador_midias/catalog/mechanisms/g1_daily.py) | Índice oficial do g1 e partições diárias conhecidas | `https://g1.globo.com/sitemap/g1/sitemap.xml` |

`g1_daily` não se aplica a qualquer RSS regional do g1, nem ao índice de vídeos ou a outro portal. Ele usa apenas filhos declarados no índice que correspondam ao padrão diário conhecido. Mudar o mecanismo não converte um documento inválido em RSS.

Parsing compartilhado em [parsing.py](../../src/buscador_midias/catalog/mechanisms/parsing.py) e descompressão em [compression.py](../../src/buscador_midias/catalog/compression.py). XML gzip/ZIP é tratado com limites; entidades externas e expansão irrestrita não fazem parte do contrato. Um índice contém referências a outros XMLs, não necessariamente notícias.

## Parâmetros configuráveis

| Chave | Padrão / limites | Como interpretar |
|---|---|---|
| `learn_temporal_routes` | 1; 0 ou 1 | Usa padrões temporais aprendidos, com inferência registrada; não comprova cobertura total |
| `archive_overlap_days` | 2; 1–7 dias | Margem das rotas temporais aprendidas |
| `lookback_days` | 0; 0–3650 dias | Janela da amostra do teste; 0 aceita qualquer data. Não substitui a janela da rodada de ingestão |
| `timeout_seconds` | 45; 5–60 segundos | Orçamento total do teste, não tempo por cada artigo |
| `max_requests` | 24; 4–48 | Requisições do teste, incluindo redirecionamentos |
| `article_samples` | 3; 0–3 | Quantidade de amostras. Zero verifica XML, sem comprovar notícia textual |
| `sample_order` | `distributed`; também `first`, `last` | Início/meio/fim ou extremos do XML. Ordem do arquivo não garante recência |
| `max_response_mib` | 8; 1–16 MiB | Limite de download e do XML descomprimido |
| `max_documents` | 12; 1–24 | Apenas modelos expansíveis; conta raiz e filhos no teste |
| `max_depth` | 10; 0–10 | Apenas modelos expansíveis; raiz está no nível 0 |
| `updates_overlap_days` | 1; 0–2 dias | Apenas `g1_daily`; margem do calendário dos arquivos |
| `updates_timezone` | `UTC` ou `America/Sao_Paulo` | Apenas `g1_daily`; calendário das partições, separado do fuso da rodada |

O mecanismo descreve campos, padrões e limites para o formulário; parâmetros desconhecidos são rejeitados. A ingestão reutiliza parsing e planejamento, mas possui execução por documento, orçamento HTTP e controle de filas próprios. Portanto, `max_documents` de um teste interativo não é um teto global de todos os XMLs de uma rodada.

## O que o botão Testar comprova

O teste faz três perguntas: conseguiu acessar o endpoint? Encontrou notícia? Confirmou texto útil? A resposta contém `checks`, `samples`, `warnings`, métricas, parâmetros, versões e `usable_for_news`.

`usable_for_news=true` exige XML válido e compatível e ao menos uma amostra textual útil. HTTP 200 sozinho não basta. Áudio/podcast dedicado, player, página de seção, erro, bloqueio ou texto insuficiente não confirmam o caminho. Uma reportagem com áudio embutido pode ser textual; o player não é automaticamente a notícia.

O botão interativo força `lookback_days=0`, independentemente do valor salvo no modelo;
o executor compartilhado pode receber outra janela nas validações finitas.

A idade da amostra é independente do acesso e da legibilidade. News sitemap antigo pode funcionar, se a janela do teste admitir sua publicação. Feed válido sem entradas recebe diagnóstico de ausência de notícias, não de rede. Sem amostra lida, não se deve deduzir que o texto é acessível.

Os testes interativos têm execução limitada: até dois simultâneos, resultados transitórios limitados a 100 registros e prazo de retenção de 15 minutos. O histórico durável é o resultado aplicado/salvo ao link, não toda consulta transitória. Aplicar um teste exige identidade e revisões correspondentes; o cliente não pode forjar uma prova enviando um JSON arbitrário.

Referências: [testing.py](../../src/buscador_midias/catalog/testing.py), [sample_audit.py](../../src/buscador_midias/extraction/sample_audit.py), [reliability.py](../../src/buscador_midias/extraction/reliability.py).

## Transporte HTTP

| `access_transport` | Conexão |
|---|---|
| `direct` | Leitor HTTP direto (`TestReader`) |
| `curl_chrome_h2` | Perfil Chrome via curl, HTTP/2 |
| `curl_chrome_h1` | Perfil Chrome via curl, HTTP/1 |
| `curl_firefox_h2` | Perfil Firefox via curl, HTTP/2 |
| `curl_bot_h2` | Perfil bot via curl, HTTP/2 |
| `curl_bot_h1` | Perfil bot via curl, HTTP/1 |

A fábrica [make_reader](../../src/buscador_midias/catalog/http_transport.py) é usada pelo teste e pelo buscador. O perfil salvo é escolhido explicitamente; a ingestão não tenta automaticamente todos os seis perfis em sequência. Os scripts de recuperação comparam perfis para atribuir um comprovado ao link.

O leitor direto mantém conexões/cookies públicos de sessão em memória, valida escopo de cada redirecionamento, resolve IP público e preserva Host/SNI e verificação de certificado. Não usa credenciais do navegador do usuário. O leitor curl implementa os perfis de transporte com `curl_cffi`, dentro dos mesmos limites de escopo e tamanho. Não é execução de página em navegador.

No fluxo atual do catálogo/ingestão, não há consulta obrigatória a `robots.txt`; implementações históricas em `net` e `strategy` têm regras distintas. DNS privado, redirecionamento fora do escopo, falhas TLS e conteúdo inválido continuam sendo rejeitados.

A ingestão mantém trava por host e respeita `Retry-After`. HTTP 403 pode ser definitivo, com exceção específica de sessão pública atualizada no acesso direto. HTTP 429/503 e rede podem admitir retentativa limitada. O fato de abrir no navegador não comprova que a resposta HTTP sem JavaScript/cookies será idêntica.

Bright Data está **desativado na ingestão de notícias**: sua configuração rejeita habilitação e a captura recebe `bright_data=None`. Testes/saúde conservam suporte ao fallback no transporte `direct`, com credenciais, orçamento e métricas; outros perfis não ativam essa cadeia. **No teste interativo, omitir `bright_data` usa a disponibilidade das credenciais como padrão**, podendo habilitar o fallback; envie `false` para garantir teste sem esse provedor. Nas rotinas de saúde, a configuração persistida controla a opção. Não confundir suporte existente com uso habilitado na rodada. OpenRouter é usado na etapa de contexto, não para abrir o RSS.

## Saúde das fontes

As duas rotinas usam o mesmo `run_test`, sem exigir notícia recente:

| Rotina | Universo |
|---|---|
| `active` | Links efetivamente ativos, para conferir saúde |
| `recovery` | Inativos elegíveis por erro de acesso; opções manuais ampliam o escopo para inativos ou exclusões automáticas por acesso |

Configuração por rotina: habilitação, intervalo em minutos (1–525600), workers (1–32), tentativas (1–3), fallback opcional e orçamento Bright Data, além de `activate_recovered`. Padrões do DDL: 1440 minutos, 8 workers, 2 tentativas, fallback e reativação automática desligados. Valores salvos prevalecem.

Uma rodada congela os elegíveis e revisões. Há no máximo uma rodada aberta por tipo. O worker do painel tem liderança/heartbeat, retoma checkpoints e não expande infinitamente novos caminhos. As retentativas do worker concentram-se em `access_error`; erros internos são diagnosticados como internos, sem reprovar a fonte.

| Categoria | Nome no painel / significado |
|---|---|
| `healthy` | Funcional comprovado: amostra textual confirmada |
| `invalid` | Formato ou mecanismo inválido: XML ilegível ou incompatível com o modelo |
| `no_news` | Acessível, sem notícias na amostra: documento lido, sem entradas úteis encontradas |
| `no_text` | Sem notícia textual confirmada: houve acesso a amostra, mas não texto útil comprovado |
| `access_error` | Falha de acesso: raiz ou amostra apresentou bloqueio/rede/TLS/HTTP |
| `inconclusive` | Evidência insuficiente para concluir; pode incluir orçamento/provedor indisponível |

Esses rótulos são diagnósticos por link, não uma sentença sobre todas as notícias do site. A precedência exata está em [health_policy.py](../../src/buscador_midias/catalog/health_policy.py).

A saúde registra resultados sem excluir automaticamente todos os links reprovados. Recuperação aprovada só ativa quando a opção está habilitada e as revisões/proveniência permitem. Exclusões/inativações manuais são preservadas; restauração de exclusão automática exige o caminho específico de recuperação. Ela não redefine intervalos nem parâmetros globais.

## Cadência observada

[cadence.py](../../src/buscador_midias/catalog/cadence.py) usa datas de publicação por URL. Com menos de três amostras ou sem amplitude temporal, retorna `unknown`. Caso contrário, usa a mediana dos intervalos positivos, taxa observada por dia e confiança baixa; confiança média exige ao menos 20 amostras e sete dias de amplitude. Datas de RSS podem ser atualização. Essa estimativa deve orientar revisão humana da agenda, não prometer publicação em horário exato.
