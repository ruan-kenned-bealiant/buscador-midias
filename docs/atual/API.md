# Interface e API local

Implementação: [dashboard/server.py](../../src/buscador_midias/dashboard/server.py), [catalog_api.py](../../src/buscador_midias/dashboard/catalog_api.py), [ingestion/api.py](../../src/buscador_midias/ingestion/api.py). A interface é HTML/JavaScript servido por `ThreadingHTTPServer`, sem etapa de build de frontend.

## Telas

| Rota | Uso |
|---|---|
| `/` e `/gerenciador` | Cadastro, filtros, paginação e links de cada site |
| `/mecanismos` | Parâmetros globais dos mecanismos |
| `/saude` | Configurações e histórico das verificações de saúde |
| `/guia` | Dicionário didático, exemplos e explicação do fluxo |
| `/buscador` | Configuração, rodadas, filas/etapas, tarefas e progresso por site |
| `/evidencias/corpus/{hash}` | Evidência de artigo relacionado à ingestão e ao corpus |
| `/testes/prioritarios`, `/testes/feeder`, `/testes/feeder-pendentes` | Relatórios estáticos de auditorias, quando seus artefatos existem |

Os HTMLs/JSs ficam em [dashboard](../../src/buscador_midias/dashboard). O gerenciador reutiliza `manager.html` para suas abas; `search.html/search.js` implementam o buscador. Recursos são carregados na criação do servidor: alterações nesses arquivos precisam ser carregadas por uma nova instância do painel. Não é necessário acessar o frontend PowerDD para usar estas telas.

## Convenções e proteção das escritas

Prefixo: **`/api/gerenciador`**. JSON em UTF-8. Datas são ISO 8601; IDs numéricos de cadastro diferem dos IDs hexadecimais de rodada/tarefa/teste. Custos decimais podem ser serializados como string para preservar precisão.

GET `/resumo` fornece `csrf_token`. POST/PATCH/DELETE precisam de `X-Catalog-Token`, `Content-Type: application/json`, corpo objeto JSON e origem local compatível com Host/porta. `Transfer-Encoding` é rejeitado e o corpo tem limite de 32.768 bytes. O token é da instância do servidor; após reiniciar, recarregue a tela.

Edições/exclusões usam `version`. Uma versão antiga produz 409, em vez de sobrescrever alteração concorrente. Não há login nem autenticação de usuários. GETs não exigem o token; mantenha o serviço local.

| HTTP | Uso típico |
|---|---|
| 200 / 201 | Consulta/alteração concluída / cadastro criado |
| 202 | Teste, rodada ou reprocessamento aceito; consultar andamento depois |
| 400 | Campos, limites, IDs ou JSON inválidos |
| 403 | Origem, Host ou token não autorizado |
| 404 | Recurso/endereço ausente |
| 409 | Revisão conflitante, execução já aberta ou recurso indisponível para nova rodada |
| 429 | Dois testes interativos já estão em execução |
| 413 / 415 | Corpo acima do limite / tipo de conteúdo inválido |
| 503 | Banco/armazenamento indisponível ou recurso que exige PostgreSQL |

## Cadastro e testes

Rotas abaixo são relativas ao prefixo.

| Método | Caminho | Entrada/uso |
|---|---|---|
| GET | `/resumo` | Contagens, backend e token; disponibilidade configurada do provedor |
| GET | `/fontes` | `q`, `status=all|active|inactive`, `country`, `offset`, `limit` |
| POST | `/fontes` | Cadastro de fonte |
| GET / PATCH / DELETE | `/fontes/{id}` | Consultar, editar ou excluir logicamente; escrita com revisão |
| GET | `/fontes/{id}/mecanismos` | Links da fonte; `q`, `status`, `kind=all|feed|sitemap`, `offset`, `limit` |
| POST | `/fontes/{id}/mecanismos` | Criar link; `test_id` opcional de prova emitida pelo servidor |
| PATCH / DELETE | `/mecanismos/{id}` | Editar/excluir link; `test_id` opcional no PATCH |
| GET | `/modelos` | Descritores, padrões, esquema de parâmetros e configuração |
| GET / PATCH | `/modelos/{model}` | Ler/atualizar parâmetros do mecanismo, com revisão |
| POST | `/testar` | Testar link salvo ou rascunho; retorna ID de trabalho |
| GET | `/testes/{test_id}` | Consultar andamento/resultado transitório |
| GET | `/validacao` | Situação da validação histórica do catálogo |

Na listagem do cadastro, `offset` vai de 0 a 1.000.000 e `limit` de 1 a 100 (padrão 25). O nome histórico `/mecanismos/{id}` nessa API identifica **um link**, enquanto `/modelos/{model}` identifica o mecanismo reutilizável. Não são a mesma entidade.

Campos de fonte: `name`, `url`, `country`, `language`, `notes`, `active`, `version`.
Campos de link: `url`, `kind`, `model`, `label`, `notes`, `active`, `version`,
`search_interval_seconds`, `access_transport`; `test_id` é tratado pela API ao salvar.
Teste: `source_id`, `url`, `kind`, `model`, opcionais `endpoint_id`, `version`,
`bright_data`, `access_transport`. Omitir `bright_data` no teste usa a disponibilidade das
credenciais como padrão; enviar `false` evita o fallback pago.

Os campos aceitos no CRUD e testes são validados em [catalog/store.py](../../src/buscador_midias/catalog/store.py) e [testing.py](../../src/buscador_midias/catalog/testing.py); os formulários em `manager.html` mostram os payloads efetivamente utilizados. Campos de evidência e resultados são controlados pelo servidor.

## Saúde

| Método | Caminho | Entrada/uso |
|---|---|---|
| GET | `/saude` | Visão geral, configurações e histórico; `offset` |
| GET | `/saude/rodadas/{id}` | Detalhe e resultados; `offset`, `category` |
| PATCH | `/saude/configuracoes/{active|recovery}` | Campos obrigatórios: `enabled`, `interval_minutes`, `workers`, `max_attempts`, `version`; opcionais: `bright_data`, `max_bright_requests`, `activate_recovered` |
| POST | `/saude/rodadas` | `kind`; opcionais `bright_data`, `activate_recovered`, `scope` |
| POST | `/saude/rodadas/{id}/cancelar` | Objeto vazio `{}`; cancelamento cooperativo |

`scope=errors` é o padrão. `inactive` e `auto_excluded_access` são ampliações manuais de recuperação, não opções de rotina agendada de ativos.

## Buscador

| Método | Caminho | Entrada/uso |
|---|---|---|
| GET | `/busca` | Visão geral/tarefas; `offset`, `stage`, `state`, `run_id`, `source_id` |
| GET | `/busca/fontes` | Progresso por site; `run_id`, `offset`, `search` |
| GET | `/busca/fontes/{id}/corpus` | Artigos reais da fonte na rodada; exige `run_id`; `offset` |
| GET | `/busca/noticias/{hash}` | Registro da ingestão e origens; pode recuperar o corpo no corpus |
| GET | `/busca/historico` | Rodadas arquivadas preservadas, somente leitura |
| PATCH | `/busca/configuracao` | `version` e objeto parcial `config` |
| POST | `/busca/rodadas` | `{}` para janela padrão; opcionais `link_ids`, `since`, `until` ou `day` |
| POST | `/busca/tarefas/{id}/reprocessar` | Objeto vazio `{}`; valida elegibilidade da tarefa |

`since`/`until` precisam ter fuso; `day` não pode ser combinado com o período explícito. A resposta 202 não indica que a rodada terminou. Configuração da busca tem chaves fechadas; workers devem informar as seis etapas, de 1 a 32 cada.

Exemplos de **corpo de requisição**, sem executar nada:

```json
{"version": 7, "config": {"enabled": false, "workers_enabled": true}}
```

Esse ajuste interrompe novas rodadas automáticas, mantendo workers habilitados para as existentes. `7` é apenas exemplo: use a revisão obtida na consulta, nunca copie uma revisão antiga.

```json
{"link_ids": [123], "day": "2026-10-26"}
```

Esse segundo exemplo solicita uma execução manual restrita, sujeita a elegibilidade, janela e data atual; não é um comando para disparar a coleta durante a leitura da documentação.

## Evidências

`/evidencias/corpus/{hash}` resolve o caminho a partir do vínculo com `midias_ia_articles`; não aceita caminho de arquivo arbitrário. O parâmetro `?original=1` entrega a representação HTML capturada, com política de sandbox e scripts bloqueados. Sem o parâmetro, a apresentação usa o visualizador isolado e preparação de HTML para legibilidade.

O leitor também aceita os tipos registrados HTML, PDF, PNG e JPEG, embora a captura normal desta ingestão seja HTML. O tamanho máximo de leitura é 16 MiB. Evidência não capturada/ausente retorna 404; excesso retorna 413. Arquivo local `.html.gz` é descomprimido sem mudar a chave lógica `.html`.

## APIs históricas

O servidor conserva consultas de mapa, rodada 12, seleção, snapshot e exportações. Sua origem é o estado/artefatos históricos, não o CRUD novo. Algumas páginas antigas retornam 410. O roteamento completo está em [server.py](../../src/buscador_midias/dashboard/server.py); essas APIs não devem ser usadas como contrato de escrita do catálogo ou como agenda do produto atual.
