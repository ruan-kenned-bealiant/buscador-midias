# Auditoria de cobertura por capa — 120 fontes

A comparação está preparada para a rodada `3727178f41234734aa62b1bd48243609`.
O serviço independente `buscador-midias-coverage-3727178f.service` aguarda o estado
`completed` ou `partial`, com `finished_at` preenchido. Antes disso, consulta apenas
PostgreSQL a cada 30 segundos; não abre os sites nem inicia nova ingestão.

## Alvos e referência

A lista explícita está em [coverage_priority_sites.csv](../data/coverage_priority_sites.csv):
60 fontes brasileiras e 60 internacionais, selecionadas por relevância editorial e
diversidade. É um recorte de validação, não um ranking estatístico de audiência.
Inclui fontes inativas, para não selecionar apenas os casos já bem atendidos.
IDs, URLs, estados e transporte são congelados no manifesto da auditoria.

O ponto de partida é a **capa**, independente dos RSS/sitemaps cadastrados. O auditor
extrai chamadas editoriais do HTML público, acessa candidatas do escopo da fonte e
confirma artigo textual e data. Guarda HTML comprimido, hash, instante de observação,
status HTTP, URL final e diagnóstico. Não navega recursivamente em outras seções.

Isso mede uma amostra observacional da capa, **não todas as notícias do site no dia**.
A página pode mudar entre o início da rodada, seu encerramento e a observação.
Capas dependentes de JavaScript ou bloqueadas recebem diagnóstico inconclusivo, sem
inventar notícia ou atribuir cobertura zero. Não há navegador renderizado nesta etapa.
A extração de texto/data reutiliza componentes da aplicação: esta auditoria testa a
cobertura de URLs e o funil, não valida independentemente a precisão desses extratores.
As provas permitem conferência visual/manual posterior.

## Comparação temporal e de identidade

- A publicação precisa estar dentro de `since/until` da rodada congelada.
- Publicação posterior ao `until` fica como `after_window`, nunca como falta da busca.
- Conteúdo anterior, data ausente, texto não confirmado, duplicatas e bloqueios ficam
  separados e fora do denominador de notícias comparáveis.
- Usa hash canônico, variante legada, URL final e canonical restrito ao mesmo escopo
  editorial. Não compara apenas títulos semelhantes.
- Um artigo já existente no corpus conta como coberto, sem simular nova gravação.
- O corpus é consultado com corte no instante em que foi observado o encerramento;
  novos registros posteriores não melhoram artificialmente essa comparação.
- Descoberta é consultada nas ocorrências da rodada exata, com indicação adicional
  de se veio da própria fonte ou de outra origem compartilhada.
- O detalhe da fonte registra quantos links participaram da rodada, inclusive zero.

## Métricas e diagnósticos

| Medida/diagnóstico | Interpretação |
|---|---|
| Referência comparável | Artigos textuais únicos da capa, com publicação na janela |
| Cobertura de descoberta | Encontrados nas ocorrências da rodada / referência comparável |
| Cobertura no corpus | Presentes no corpus no corte / referência comparável |
| Cobertura sem exclusões de política | Mesmo numerador, retirando do denominador os descartes explícitos por regra |
| `corpus_new` / `corpus_already_known` | Registro real recente / registro que já existia antes da rodada |
| `discovery_gap` | Referência elegível ausente da descoberta desta rodada e do corpus |
| `processing_failure` | Descoberta aconteceu, mas houve falha terminal |
| `policy_exclusion` | Descarte explícito por falta de entidade ou categoria de conteúdo |
| `extraction_or_date_gap` | A aplicação encerrou por texto/data/janela, enquanto a auditoria confirmou uma referência elegível |
| `processing_pending` | Ainda há etapa em aberto relacionada à URL |
| `processing_unresolved` | Referência elegível descoberta, sem resolução identificada |

Sem referências comparáveis, a porcentagem é **nula/inconclusiva**, não zero nem 100%.
O agregado é ponderado pelo número de notícias da amostra; o relatório também permite
examinar cada site. Sempre considerar junto o número de sites comparáveis, bloqueados
e atingidos por limites. Percentual alto em poucas capas acessíveis não comprova
cobertura alta dos 120 veículos.

## Limites e efeitos

São oito fontes em paralelo, até 200 candidatas, 300 segundos e 256 requisições por
fonte, incluindo redirecionamentos. Há no máximo dois perfis de acesso para a capa:
o perfil salvo e HTTP direto, deduplicados. A janela de tempo é compartilhada entre
eles. HTTP é limitado a 4 MiB por resposta. Provas comprimidas têm orçamento global
de 1 GiB e reserva mínima de disco de 512 MiB. Limites atingidos ficam sinalizados;
a contagem não é apresentada como varredura integral da capa.

Todas as transações PostgreSQL são `READ ONLY`. Não modifica catálogo, mecanismos,
estados de saúde, corpus, tarefas ou filas. Não chama Bright Data nem OpenRouter.
Os checkpoints por fonte permitem retomada, com lock para impedir duas instâncias
no mesmo diretório. Uma auditoria terminada não se repete indefinidamente.

## Artefatos e acompanhamento

Pasta: `var/gerenciador/cobertura-capas-3727178f/`.

| Arquivo | Conteúdo |
|---|---|
| `manifest.json` | Alvos, limites, rodada e corte da comparação |
| `status.json` | Aguardando rodada, auditando ou concluído; contagem de fontes |
| `sources/{id}.json` | Provas e resultado por fonte |
| `html/*.html.gz` | HTML de referência, separado das evidências do corpus |
| `articles.csv` | Notícia a notícia, data, URL, presença e diagnóstico |
| `summary.json` | Métricas agregadas, limites e quantidade de sites comparáveis |
| `report.html` | Relatório navegável por fonte, gerado progressivamente |

```bash
systemctl --user status buscador-midias-coverage-3727178f.service --no-pager
journalctl --user -u buscador-midias-coverage-3727178f.service -n 30 --no-pager
```

Executor: [audit_frontpage_coverage.py](../scripts/audit_frontpage_coverage.py).
Regras puras: [analysis/coverage.py](../src/buscador_midias/analysis/coverage.py).
Validação local: 19 testes sem rede/banco, Ruff e mypy nos módulos alterados.
Os resultados reais só existirão depois de a rodada encerrar e a auditoria executar.
