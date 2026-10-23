# Validação finita antes da migração para Mídias

A rodada percorre todos os links não excluídos de sites não excluídos, incluindo
inativos. A lista de associações é congelada ao iniciar: não há descoberta recursiva
de novas fontes, coleta contínua, cron ou agendamento recorrente.

## Critérios e tentativas

Um link só é conservado quando um teste real comprova acesso ao XML, compatibilidade
do mecanismo, acesso a uma notícia da fonte e texto útil extraído. HTTP 200, referências
de índice, seção HTML, XML vazio e áudio dedicado não comprovam notícia textual.

A janela configurada é de 365 dias, calculada no início de cada tentativa; ela não
fica congelada no horário de início de uma rodada longa. Publicações conhecidas fora
da janela não servem como prova. Datas desconhecidas são sinalizadas e não reprovam
sozinhas um caminho de texto comprovado. A data do artigo prevalece sobre a indicação
do XML. `lastmod` é somente uma pista para priorizar URLs/filhos, nunca data de publicação.

São até três tentativas completas por associação: mecanismo cadastrado, automático e,
se necessário, mecanismo específico do formato detectado. Um mecanismo que passar é
atribuído ao link; não se troca por outro que não tenha passado. Cada tentativa tem
até 30 segundos, 48 requisições, 24 documentos em modelos de árvore, profundidade 10 e
três páginas de notícia. Índices amostram até três folhas quando houver orçamento,
evitar condenar um índice inteiro só porque sua primeira folha traz notícias antigas.
A aprovação é por amostra; não afirma que todos os artigos do XML funcionam.

As configurações globais dos cinco modelos são salvas com a janela anual e os limites
da rodada. Cada resultado registra seus parâmetros, revisão e janela efetivamente usados.
Falhas não confirmadas após as tentativas, inclusive bloqueios/ausência de texto,
resultam em exclusão lógica. São critérios operacionais do leitor HTTP direto atual;
não afirmam que um site está definitivamente indisponível para qualquer navegador.

## Execução, segurança de dados e progresso

A fila tem até 32 hosts em paralelo, com um link de cada host por vez e espaçamento
global mínimo de 300 ms nas requisições. Robots, DNS público, conexão fixada no IP,
TLS e escopo são respeitados. Há cache em memória de respostas bem-sucedidas da própria
rodada, por até cinco minutos, limitado a 64 MiB/8.192 entradas. A raiz volta à rede nas
tentativas posteriores. Falhas de transporte não são reaproveitadas como prova positiva.
Nenhum serviço pago, Bright Data ou OpenRouter é acionado.

Os resultados são persistidos antes de aplicar exclusões. Somente após toda a fila
chegar a uma decisão as provas/mecanismos são aplicados e os links não comprovados são
excluídos logicamente. Sites sem links restantes são inativados. O histórico da rodada
12, evidências anteriores e cadastros excluídos continuam preservados.

Controles de rede independentes são conferidos no início, periodicamente e antes de
aplicar as decisões. Indisponibilidade geral pausa a rodada, em vez de excluir a base
por falha de infraestrutura. Falta de espaço também preserva a fila para retomada.
Edições concorrentes do link/site/configuração são preservadas e marcadas como conflito;
se houver conflitos, a conclusão fica explicitamente pendente de nova validação deles.

O painel mostra contadores reais e consulta `GET /api/gerenciador/validacao` a cada
cinco segundos. Os registros do cadastro só são saneados ao final. `progresso.json`
e `links.csv` são retratos exportados ao pausar/concluir; o progresso ao vivo está no
SQLite e nessa API, não no arquivo exportado anteriormente.

## Tabelas e periodicidade

- `catalog_validation_runs`: configuração, janela de referência, estado e identificação da rodada.
- `catalog_validation_links`: fila persistente, revisões originais e decisão de cada link.
- `catalog_validation_attempts`: cada tentativa, modelo, resultado e aprovação. O payload
  JSON é comprimido com zlib para limitar disco; os campos de diagnóstico são relacionais.
- `link_cadence`: estimativa atual por link, amostras, confiança e intervalo em segundos.
- `link_test_results`: a prova aplicada ao link, incluindo parâmetros e estimativa completa.
- `events`: alterações e exclusões lógicas com valores anteriores/posteriores.

A frequência usa datas por URL declaradas em RSS/news sitemap e datas extraídas dos
artigos. Menos de três datas ou ausência de intervalo resulta em `unknown`. O intervalo
é a mediana entre datas distintas; a taxa observada de notícias/dia e a abrangência da
amostra também ficam salvas. Confiança é baixa ou moderada, nunca garantia de atualização.
Datas de RSS podem representar atualização. Não se cria agenda automática a partir disso;
a configuração da frequência de busca será uma etapa posterior.

## Iniciar e retomar

Na raiz do projeto, para uma nova rodada:

```bash
PYTHONPATH=src .venv/bin/python scripts/validate_catalog.py --workers 32
```

Para continuar a mesma rodada após pausa/desligamento, sem recomeçar os concluídos:

```bash
PYTHONPATH=src .venv/bin/python scripts/validate_catalog.py --resume ID_DA_RODADA --workers 32
```

Um bloqueio de arquivo impede dois processos usando a mesma fila ao mesmo tempo. Um
backup consistente do SQLite é criado antes de iniciar uma nova rodada. A retomada
utiliza as configurações e as tentativas persistidas; não recria links já excluídos.
Após desligamento, a execução precisa ser iniciada novamente; não foi instalado serviço
de inicialização automática. A integração com o banco definitivo de Mídias segue futura.
