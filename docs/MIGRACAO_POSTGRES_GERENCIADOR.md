# Gerenciador no PostgreSQL de Mídias

O gerenciador utiliza a mesma conexão `DB_MIDIAS_IA_HOST/PORT/DATABASE/USERNAME/PASSWORD`
da base de Mídias. O destino desta instalação é `power_dd_midias`, esquema
`midias_manager`. O esquema separa o cadastro das tabelas existentes de notícias e
do crawler. Nenhuma dessas tabelas anteriores é sobrescrita.

## Estrutura a transportar para o powerDD

O DDL versionável está em `catalog/postgres_schema.py`. O Python cria tabelas,
chaves estrangeiras, índices e sequências; depois essas definições podem virar
migrations nativas do powerDD, preservando o esquema e os identificadores.

| Tabela no esquema `midias_manager` | Conteúdo |
|---|---|
| `sites` | Cadastro, estado, revisão e exclusão lógica dos sites |
| `links` | RSS/sitemap, site pai, mecanismo, evidência e intervalo de busca |
| `mechanisms` | Identificador do executor, parâmetros globais e revisão |
| `link_test_results` | Histórico imutável dos testes salvos |
| `events` | Auditoria com estados anterior e posterior |
| `metadata` | Proveniência e metadados do catálogo |
| `schema_migrations` | Histórico das migrações locais já aplicadas |
| `catalog_validation_runs` | Rodadas de validação |
| `catalog_validation_links` | Fila e decisões por link/rodada |
| `catalog_validation_attempts` | Tentativas e resultados históricos comprimidos |
| `link_cadence` | Estimativa de periodicidade de publicação, separada do intervalo de busca |
| `catalog_imports` | Manifesto da importação PostgreSQL, contagens e hashes |

IDs usam `BIGINT` com identidade para novos registros. As relações obrigatórias
são `links.source_id → sites.id` e `links.model → mechanisms.model`; testes e
validações também têm FKs. Evidências, parâmetros e instantâneos usam `JSONB`,
datas usam `TIMESTAMPTZ` em UTC. `active`/`usable` usam `SMALLINT CHECK IN (0,1)`
para compatibilidade com o contrato atual; os modelos do powerDD podem expô-los
como booleanos. `search_interval_seconds` mantém os intervalos existentes e o
padrão de 4 horas; o gerenciador os edita em minutos, horas ou dias.

O campo `catalog_validation_attempts.result` é `BYTEA` contendo exatamente o
JSON comprimido com zlib da origem. Para lê-lo no powerDD, descomprimir antes de
interpretar JSON; não tratar como JSONB. Os demais resultados de teste são JSONB.

O código dos mecanismos permanece em arquivos Python; a tabela guarda a escolha
e os parâmetros, sem código executável. A migração posterior da aplicação para o
powerDD deve preservar o mesmo contrato e o controle otimista por `version`.

## Comandos e corte

```bash
scripts/bm.sh migrate-catalog var/gerenciador/catalogo.sqlite \
  --report var/gerenciador/migracao-postgres/resultado.json --activate
```

O comando usa um retrato consistente da origem, verifica integridade e copia as
11 tabelas em uma única transação. Confere contagens e hashes de todas as linhas,
normalizando somente JSON e datas para comparar os tipos nativos. Mantém IDs,
exclusões, estados e revisões, e ajusta as sequências. Uma falha desfaz a criação
do esquema e a importação. Um esquema preexistente sem manifesto é recusado.
Uma segunda execução apenas confere: não sobrescreve edições do PostgreSQL.

`--activate` escreve atomicamente `var/gerenciador/storage.json` somente depois
do sucesso. Reiniciar o painel aplica o corte. Durante a cópia final, manter o
painel parado e impedir outros escritores do SQLite. Para apenas conferir uma
origem que ainda represente exatamente o destino:

```bash
scripts/bm.sh migrate-catalog var/gerenciador/catalogo.sqlite --check-only
```

Depois de novas edições no PostgreSQL, a divergência em relação ao SQLite é
esperada: esse arquivo é o retrato de segurança do momento do corte, sem novas
escritas pelo painel. Não executar rotinas antigas que alterem esse arquivo.
Falha no PostgreSQL gera erro de disponibilidade; não existe retorno automático,
sincronização ou escrita dupla no SQLite. O comando Python independente também
está em `python -m buscador_midias.catalog.migrate_postgres` (requer ambiente
`DB_MIDIAS_IA_*`; o corte pelo arquivo de configuração é responsabilidade do CLI).

Arquivos brutos de mapeamento e da rodada 12 continuam nos diretórios históricos
existentes. O catálogo e seu histórico relacional são transportados integralmente;
esses arquivos externos não são convertidos em tabelas de notícias.

## Validação

Os testes reais usam esquemas descartáveis com prefixo `bm_test_catalog_` e exigem
`BM_TEST_POSTGRES=1` mais as credenciais da base de teste. Verificam importação,
conferência repetida, rollback, CRUD, duplicação, índices de identidade, testes
textuais, intervalos, buscas com acentos e edições concorrentes. Os esquemas de
teste são removidos ao terminar; o esquema ativo não é usado para esses testes.
