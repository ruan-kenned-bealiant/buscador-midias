# Recuperação de espaço — 27/10/2026

O disco estava sem espaço disponível para o usuário. Foram removidos somente os
caches regeneráveis de downloads pip/Composer e de conteúdo do Chrome, sem remover
perfil, cookies, dependências instaladas ou credenciais.

A ingestão foi pausada para compactar **33.018 evidências HTML** sem alteração há
mais de uma hora. Cada arquivo gzip foi relido e teve seu SHA-256 comparado com o
conteúdo original antes de retirar a representação bruta. A compactação economizou
**10.832.159.399 bytes (10,09 GiB)**. Ao terminar, havia aproximadamente **11 GB
livres** no sistema de arquivos, além de flutuações das outras aplicações.

Nenhum registro do banco, evidência, histórico de rodada ou volume Docker foi
excluído. O leitor mantém as chaves `.html` do corpus e encontra a cópia `.html.gz`.
A compactação anterior de 26/10 permanece preservada em seu próprio diretório.

## Rastreabilidade e validação

- `var/maintenance/space-20261027/compressed.jsonl`: caminho, tamanho original,
  tamanho comprimido, hash original e mtime de cada arquivo.
- `var/maintenance/space-20261027/summary.json`: contagem e espaço recuperado.
- `var/maintenance/space-20261027/reading-verified.json`: três arquivos reais
  reabertos pelo leitor da aplicação e conferidos contra os hashes originais.
- Os 12 testes de `tests/evidence/test_storage.py` passaram.

O compactador aceita `--min-age-hours` (padrão 24, mínimo 1) e passa a separar os
logs por data UTC, evitando sobrescrever o resumo de outra manutenção. Nesta
execução foi usado `--apply --min-age-hours 1 --target-free-gib 12`; todos os
candidatos elegíveis terminaram antes de atingir a meta de 12 GiB livres. O comando
não é agendamento automático nem altera como novas evidências são gravadas.

O Docker/PostgreSQL ainda não respondiam após a recuperação do espaço; foi solicitado
reinício do Docker Desktop sem exclusão de contêineres ou volumes. A restauração dos
serviços é verificada separadamente antes de declarar a auditoria retomada.

## Retomada confirmada

Após iniciar novamente o serviço do Docker Desktop, PostgreSQL e RabbitMQ voltaram
a responder. Os serviços `buscador-midias-ingest.service` e
`buscador-midias-coverage-frozen-3727178f.service` foram retomados. O agendamento
de novas rodadas continua desligado (`enabled=false`) e os trabalhadores
continuam habilitados. A rodada existente ainda estava em `running` na retomada.
