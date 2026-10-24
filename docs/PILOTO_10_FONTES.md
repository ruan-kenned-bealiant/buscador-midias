# Piloto ao vivo em dez fontes — 24/10/2026

Foram testados dez sites, inicialmente um link por site, com janela fixa de 48 horas e duas consultas por link. Na CNN houve uma verificação adicional de outro link já cadastrado, pois o primeiro entregou uma amostra antiga. Ao todo: 11 links, 22 consultas e 36 amostras de páginas.

**Resultado:** todos os dez links iniciais forneceram XML/RSS e URLs. Nove sites tiveram pelo menos uma notícia textual recente confirmada na amostra após a conferência adicional da CNN. A NPR forneceu URLs, mas suas três páginas amostradas não puderam ser lidas. Isso não comprova cobertura completa dos sites.

## Medições por fonte

A tabela usa o segundo link da CNN, `/sitemap/3646.xml`. Os tempos de descoberta incluem a comparação no corpus; os tempos totais incluem a leitura de até três páginas. Repetições sem URLs novas não baixam novamente as notícias.

| Fonte | URLs 1ª → 2ª | Descoberta + dedup 1ª | Total 1ª / 2ª | Textos recentes na amostra | Novas tarefas na 2ª |
|---|---:|---:|---:|---:|---:|
| Folha de S.Paulo | 7 → 7 | 0.829 s | 2.010 / 0.267 s | 3 | 0 |
| O Globo | 720 → 720 | 8.737 s | 11.038 / 0.617 s | 3 | 0 |
| O Estado de S. Paulo (Estadão) | 20 → 20 | 1.430 s | 3.361 / 0.394 s | 3 | 0 |
| CNN Brasil | 504 → 572 | 5.681 s | 7.558 / 2.085 s | 4 | 68 |
| Agência Brasil | 10 → 10 | 1.953 s | 3.406 / 0.719 s | 3 | 0 |
| Poder360 | 10 → 10 | 0.982 s | 2.332 / 0.399 s | 3 | 0 |
| Metrópoles | 609 → 609 | 8.116 s | 9.923 / 1.258 s | 3 | 0 |
| NPR | 9 → 9 | 2.272 s | 23.943 / 0.363 s | 0 | 0 |
| The Guardian | 420 → 420 | 7.243 s | 9.778 / 0.411 s | 2 | 0 |
| Al Jazeera English | 264 → 264 | 3.261 s | 4.923 / 0.392 s | 2 | 0 |

As rodadas somaram **98,112 segundos de processamento medido** (88,469 s na seleção inicial e 9,643 s na conferência da CNN), além da preparação e das consultas de diagnóstico. Foram registradas **3.641 URLs únicas**, incluindo o arquivo antigo da CNN; esse número não equivale a notícias recentes nem a textos coletados.

Das 36 páginas amostradas: **26 textos recentes confirmados**, 5 textos fora da janela, 2 páginas sem texto suficiente e 3 falhas de acesso na NPR. Notícias antigas foram classificadas como fora da janela, sem erro de acesso. Dos conteúdos sem texto suficiente, um era uma página ao vivo do Guardian e outro era uma página de vídeo da Al Jazeera.

## Deduplicação e acesso

- Nas vinte consultas iniciais, cada repetição gerou zero novas tarefas de deduplicação.
- No segundo link da CNN, a lista passou de 504 para 572 URLs. A repetição criou somente 68 tarefas novas, sem reenfileirar as 504 conhecidas.
- O primeiro link da CNN, `/sitemap/1.xml`, trouxe 1.000 URLs. As três páginas amostradas eram de 2020/2021. É um arquivo funcional, mas inadequado como única rota de atualização.
- Uma única leitura do índice oficial da CNN encontrou 542 filhos em 0,660 s, sem percorrê-los. O endereço `/sitemap/3646.xml` já estava no catálogo e foi confirmado no índice. Sua amostra tinha datas mistas; `lastmod` não garante publicação recente.
- Na NPR, o feed funcionou. A tentativa de obter o robots das páginas não recebeu status HTTP no acesso direto e o fallback Bright Data retornou erro do proxy 407. A causa não foi atribuída definitivamente ao site.

## Escopo e preservação

O piloto usou os mecanismos cadastrados: RSS, news sitemap e sitemap. O mecanismo específico `g1_daily` não foi aplicado a portais com estruturas diferentes. O teste demonstra o benefício de consultar uma lista de atualização conhecida antes de buscar textos, sem afirmar que todo índice genérico já possui uma estratégia temporal aprendida.

A deduplicação consultou o corpus real. Não houve execução das filas de contexto, evidência ou persistência, nem chamadas OpenRouter ou inserções no corpus. As tarefas posteriores e os HTMLs amostrados ficaram preservados nos schemas e diretórios isolados do piloto.

O serviço geral permanece **inativo**, `enabled=false`, `workers_enabled=false`, sem workers gerais vivos. As 1.318 notícias persistidas anteriormente e as filas históricas foram preservadas. O catálogo principal não foi alterado.

## Evidências e reprodução

- CSV por fonte: `var/ingestion/piloto-10-fontes/resumo.csv`.
- Seleção inicial: `var/ingestion/piloto-10-fontes/bm_sources_pilot_20261024_192553_32935f/resultado.json`.
- Conferência CNN: `var/ingestion/piloto-10-fontes/bm_sources_pilot_20261024_192728_6a42ca/resultado.json`.
- Índice CNN: `var/ingestion/piloto-10-fontes/cnn-conferencia/index.json` e `index.xml.gz`.
- Diagnóstico NPR: `var/ingestion/piloto-10-fontes/npr-diagnostico.json`.
- Auditoria de isolamento e contagens: `var/ingestion/piloto-10-fontes/auditoria.json`.

```bash
scripts/bm.sh ingest sources-pilot --link-ids 40462 40464 254 1121 1436 2379 40492 12406 15949 36426 --samples 3
scripts/bm.sh ingest sources-pilot --link-ids 1123 --samples 3
```

O comando exige a ingestão geral pausada, copia apenas os cadastros escolhidos e cria schema e filas Rabbit exclusivos. São duas consultas finitas, com uma tentativa por tarefa e até três páginas por link e consulta. O histórico de testes de produção não é substituído.
