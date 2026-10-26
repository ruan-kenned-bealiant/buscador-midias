# Recuperação de links por transporte HTTP — 26/10/2026

Rodada `8d8a0869538544cd99d29ea4b33704fe` concluída em 2:02:00.

Foram examinados **22.759 links** de uma lista congelada: excluídos automaticamente,
links ativos com falhas anteriores e os candidatos XML oficiais do piloto.
Houve confirmação textual independente em **11.128 links**,
pertencentes a **505 fontes**.
Itens ainda adiados por pausa: **43**. Permanecem
sem conclusão; o total examinado não significa que todos chegaram à rede.

Um link comprovado pode duplicar outro já cadastrado; por isso confirmação e restauração
têm números diferentes.

| Decisão aplicada | Links |
| --- | ---: |
| Restaurados e ativados | 10.547 |
| Duplicatas preservadas sem nova ativação | 217 |
| Links ativos com acesso configurado | 362 |
| Novos links cadastrados | 2 |

O complemento canônico configurou mais **7 links**
ativos que estavam no transporte padrão, sem restaurar duplicatas. Seus checkpoints
estão em `canonical-recheck/`.

Foram reativadas **118 fontes** automaticamente inativadas.
O catálogo ficou com **28.599 links ativos**
em **880 fontes ativas**.
Esses totais incluem os links existentes antes desta rodada, que não foram todos revalidados aqui.

## O que foi testado

Até três perfis: curl com Chrome HTTP/2, Chrome HTTP/1.1 e Firefox HTTP/2.
Cada perfil tem até 25 segundos, 16 requisições, quatro documentos XML,
profundidade máxima dez, resposta de até 8 MiB e três artigos distribuídos na amostra.
404/410, Retry-After, RSS vazio e falhas puramente editoriais encerram tentativas desnecessárias.
Há até 32 grupos de hosts em paralelo. Nos grupos grandes, até dois links ficam
em trânsito; Prensa Libre e Information permitem até quatro após centenas de
aprovações sem pausas. A cauda da OCCRP permitiu até três leituras em trânsito.
Os grupos pequenos são sequenciais.
O espaçamento mínimo por host é de 300 ms.

A aprovação exige XML compatível, artigo acessível e texto útil. Uma nova sessão, sem
cache da primeira leitura, confirma um artigo. Essa prova independente pode ser
reutilizada por até cinco minutos entre feeds da mesma fonte/perfil/artigo, preservando
o horário original e registrando `reused_at`. Datas antigas são aceitas para comprovar
funcionamento. Isso não comprova que o feed atualiza diariamente nem que todas as suas
notícias estejam acessíveis. Áudio dedicado, CAPTCHA e páginas sem texto não são aprovados.
Pausas curtas podem ser aguardadas antes do próximo link, com teto acumulado de
180 segundos por grupo de host. Os itens somente adiados tiveram uma retomada limitada;
os registros anteriores estão em `before-cooldown-retry/`. Itens ainda adiados
continuam sem conclusão e não são classificados como definitivamente inválidos.
A OCCRP recebeu uma correção específica de reconhecimento do corpo/data editorial;
os falsos negativos anteriores estão preservados em `before-occrp-template-fix/`.

## Aplicação e uso

A coluna `links.access_transport` conserva o perfil comprovado. Teste do gerenciador,
Saúde das fontes e futuras tarefas de descoberta usam esse mesmo valor. Ele pode ser
editado em **Forma de acesso**. O mecanismo continua responsável por interpretar XML e
selecionar notícias; o perfil configura a conexão HTTP.

Exclusões manuais e alterações concorrentes são preservadas. URLs equivalentes não são
restauradas em duplicidade. Nos links aplicados, testes anteriores e novos permanecem
em `link_test_results`,
com eventos de auditoria e confirmação independente. Os demais resultados desta
rodada ficam nos checkpoints comprimidos e nos CSVs. A auditoria SQL conferiu
10.918 links alterados; divergências: 0.

Arquivos históricos e índices extensos ficam identificados como `archive`/`review`,
se não havia classificação anterior, para evitar busca histórica na rotina diária.
Finalidades dos links aplicados: `{'updates': 4545, 'review': 306, 'archive': 6060}`.

Não houve Bright Data, OpenRouter, análise de contexto ou nova rodada de ingestão.
A agenda automática continua no estado verificado em `summary.json`.
HTTP/2 não resolveu todos os sites: persistem bloqueios, endereços inválidos, feeds vazios,
falhas de rede e páginas sem texto. Ganhos também incluem revalidação atual e correção de
extração; não são atribuídos exclusivamente ao protocolo HTTP/2.

## Artefatos e reprodução

`var/gerenciador/recuperacao-transportes-20261026/` contém manifesto, resultados
comprimidos por link, decisões aplicadas, `links.csv`, `fontes.csv` e `summary.json`.
Na rodada principal, os corpos positivos são preservados comprimidos até 160 MiB; os
resultados estruturados não são cortados por esse limite. Há checkpoints para retomada.

```bash
PYTHONPATH=src .venv/bin/python scripts/recover_all_transports.py
PYTHONPATH=src .venv/bin/python scripts/apply_transport_recovery.py --apply
PYTHONPATH=src .venv/bin/python scripts/recheck_canonical_transports.py
PYTHONPATH=src .venv/bin/python scripts/report_transport_recovery.py
```

Executar novamente reutiliza a lista e os checkpoints desta rodada. Não amplia
automaticamente o universo de links nem abre novas rodadas de descoberta.
