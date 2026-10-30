# Tema por slug: um parecer por entidade e tema, sem duplicar por `theme_id`

Plano do próximo card. O buscador já grava o parecer de cada entidade por tema, mas hoje grava **uma
linha por `theme_id` do KyX**. Este documento diz o que mudar no KyX, no PowerDD e no buscador, e em
que ordem, para passar a gravar **uma linha por slug**.

## Por que mudar

`midias_ia_article_contexts` é a tabela de pareceres compartilhada entre o KyX, o PowerDD e o
buscador. O tema da análise é o **slug** (`midias_ia_theme_prompts.slug`, por exemplo `corrupcao`).
O KyX, porém, tem um tema por tipo de pessoa e por nacionalidade (`temas`: PF nacional, PJ nacional,
PF estrangeira, PJ estrangeira). Uma menção só traz o nome, então o buscador não sabe qual variante a
busca vai usar e grava as duas da pessoa (nacional e estrangeira):

```
Notícia N, entidade "Maria Souza" (PF), tema corrupcao
  linha 1: theme_id = 1  (Corrupção PF nacional)
  linha 2: theme_id = 5  (Corrupção PF estrangeira)     <- mesmo parecer, texto idêntico
```

Cada parecer existe, portanto, duas vezes. Em outubro/2026 eram ~729 mil linhas com tema gravadas pelo
buscador para ~366 mil pareceres distintos (a tabela inteira tinha ~1,4 milhão de linhas e 2,9 GB).
Gravar por slug elimina a metade repetida e deixa de depender de o buscador conhecer os ids do KyX.

## Desenho (opção A: coluna `theme_slug`)

- Coluna nova `theme_slug varchar(40) NULL` em `midias_ia_article_contexts`.
- Tabela nova `midias_ia_theme_slugs (theme_id bigint PRIMARY KEY, slug varchar(40) NOT NULL)`: cada
  `temas.id` do KyX com o slug a que pertence. É o conteúdo de `corpus/theme_catalog.json` do
  buscador (`map`), semeado por migration. Quem muda o catálogo de temas do KyX atualiza esta tabela.
- A linha gravada pelo buscador passa a ter `theme_slug` preenchido e `theme_id` **nulo**; `theme` e
  `theme_description` seguem com o nome e a descrição do tema (os do catálogo, sem a variante).
- A linha do KyX continua com `theme_id` do KyX e passa a ter também `theme_slug` (resolvido pela
  tabela), para as duas formas se encontrarem.
- Slug sem tema no KyX (hoje `processos_judiciais`) continua sem `theme_slug` e sem `theme_id`, como
  hoje: cai no balde "Não identificado". Não mudar isso neste card.

### Como a busca acha o parecer

A busca registra os `theme_id` do KyX que rodou (`themes[].theme_id`, no PowerDD; `theme_ids` nos
snapshots). A regra nova é: **a linha vale para a busca se o `theme_id` dela está nos ids da busca
OU se o `theme_slug` dela é o slug de algum desses ids.**

```
ids da busca = {1, 3}            -> slugs = {corrupcao, financeiro}   (por midias_ia_theme_slugs)
linha theme_id=1                 -> vale (id)
linha theme_id=NULL, slug=financeiro -> vale (slug)
linha theme_id=NULL, slug=trabalhista -> não vale
```

## O que muda no PowerDD (`powerdd-backend`)

1. **Migration**: coluna `theme_slug`, tabela `midias_ia_theme_slugs` (com seed), índice em
   `theme_slug` e a **troca do índice único**. O atual
   (`2026_08_27_000004_create_midias_ia_article_contexts.php`) é
   `midias_ia_article_contexts_scope_unique (article_id, COALESCE(theme_id, 0), entity_hash)`; com
   `theme_id` nulo, todos os slugs de uma entidade colidiriam. O novo é
   `(article_id, COALESCE(theme_id, 0), COALESCE(theme_slug, ''), entity_hash)`. Criar o novo antes
   de apagar o antigo.
2. **Backfill** de `theme_slug` nas linhas existentes: `UPDATE ... FROM midias_ia_theme_slugs`
   casando por `theme_id`. Em lotes por faixa de `id`.
3. **`MidiasIaArticleContext::scopeDisplayable`**
   (`app/Models/MidiasIa/MidiasIaArticleContext.php`):
   - `whereIn('c.theme_id', $themeIds)` vira `theme_id IN (ids) OR theme_slug IN (slugs dos ids)`;
   - no filtro por snapshot (`c.theme_id IS NULL OR c.theme_id = ANY(s.theme_ids)`), a linha de slug
     tem `theme_id` nulo e **não pode** cair no "sem restrição de tema": casar por
     `theme_slug IN (slugs de s.theme_ids)`. O "nulo = sem restrição" fica só para quem não tem nem
     `theme_id` nem `theme_slug`;
   - o ramo "tema que nenhum snapshot cobre" (`snapshotThemeIds`) precisa tratar o slug do mesmo modo.
4. **`MidiasNegativasRepository`** (`app/Repositories/MidiasNegativasRepository.php`):
   - contagem de temas distintos (`distinct('theme_id')`): contar por `COALESCE(theme_slug, theme_id)`;
   - filtro `themes` (`orWhereIn('theme_id', ...)` e `orWhereNull('theme_id')`): aplicar a regra
     acima; o "Não identificado" passa a ser `theme_id IS NULL AND theme_slug IS NULL`;
   - `themeDistribution` (agrupa por `theme_id, theme, theme_description`): agrupar por slug e
     devolver, em `theme_id`, **o id do tema da própria busca** que tem aquele slug. Assim o front
     continua mandando ids e **não precisa mudar**;
   - `themeIdsOf`: sem mudança (continua lendo `themes[].theme_id` da busca).
5. Model `MidiasIaArticleContext` (`$fillable`/casts) e o seed de testes
   (`database/seeders/data/midias-ia/midias_ia_article_contexts.sql`).
6. Testes: um caso por forma de linha (só `theme_id`, só `theme_slug`, as duas, nenhuma) nos filtros,
   na distribuição e no "Não identificado".

## O que muda no KyX (`KyX-NovoDD-backend`)

1. **`MidiasIaArticlePersistenceService`**: o `ON CONFLICT (article_id, COALESCE(theme_id, 0),
   entity_hash)` (linha do `INSERT ... ON CONFLICT`, e o comentário sobre o índice) passa a usar o
   índice novo, `(article_id, COALESCE(theme_id, 0), COALESCE(theme_slug, ''), entity_hash)`.
   **Se o índice antigo for apagado e este trecho não mudar, toda gravação do KyX falha** com "no
   unique or exclusion constraint matching the ON CONFLICT specification". Os dois entram juntos no
   mesmo deploy.
2. Gravar `theme_slug` nas linhas do KyX (consulta a `midias_ia_theme_slugs` pelo `theme_id`) e
   ordenar o lote por `(article_id, theme_id, theme_slug, entity_hash)`, como já faz para o `theme_id`,
   para não causar deadlock.
3. **`scopeDisplayable`** do `Models/MidiasIa/MidiasIaArticleContext.php` (cópia do PowerDD): mesma
   regra do item 3 do PowerDD.
4. **`ProcessController`** (leituras de `midias_ia_article_contexts` com `theme_id`, como
   `'themeId' => $context->theme_id`): o tema de uma linha de slug vem do `theme_id` do processo cujo
   slug é o dela; resolver pela tabela, não pela coluna.
5. Model e testes, como no PowerDD.

## O que muda no buscador (`buscador-midias`)

Só depois dos dois itens acima estarem em produção (ver a ordem abaixo):

- `corpus/contexts.py`: uma linha por entidade e slug (hoje, uma por entidade e `theme_id`), com
  `theme_slug` e `theme_id` nulo; o `ON CONFLICT` usa o índice novo. `article_themes` devolve os
  slugs com nome e descrição do catálogo, sem as variantes.
- `corpus/theme_catalog.py`: o catálogo passa a dar nome e descrição por slug; `slug_of(theme_id)`
  segue existindo para ler linhas antigas.
- `db/articles.py`: o tema da linha vem de `theme_slug` e, só se for nulo, de `slug_of(theme_id)`.
- Tela Artigos e testes (`tests/corpus`, o DDL do corpus de teste com a coluna e o índice novos).
- **Limpeza** das linhas repetidas (abaixo).

## Ordem do deploy

A ordem importa: gravar por slug antes de os leitores entenderem o slug faz os pareceres sumirem da
listagem.

1. Migration (coluna, tabela de slugs, índice novo) e backfill de `theme_slug`.
2. PowerDD e KyX publicados lendo **as duas formas** e gravando com o índice novo. Nada muda para o
   usuário: as linhas antigas continuam valendo por `theme_id` ou por `theme_slug`.
3. Remover o índice antigo (só agora; o KyX já usa o novo).
4. Buscador passa a gravar por slug.
5. Limpeza das linhas antigas do buscador (SQL abaixo), com `pg_dump` antes.
6. Conferir a listagem de uma busca nacional e de uma estrangeira antes e depois.

## Limpeza das linhas antigas

O buscador é o único produtor das linhas a converter (`raw_context->>'producer' = 'buscador-midias'`,
sem `process_id`, `client_id` e `group_id`). Por artigo e entidade, fica **uma** linha por slug,
vira linha de slug (`theme_id` nulo), e as outras saem. Em lotes por faixa de `article_id`:

Primeiro sai a linha antiga cujo parecer o buscador já regravou por slug (reprocessada depois do passo
4); sem isso a conversão abaixo bateria no índice único:

```sql
DELETE FROM midias_ia_article_contexts o
USING midias_ia_article_contexts n
WHERE o.article_id BETWEEN :de AND :ate
  AND o.theme_id IS NOT NULL AND o.process_id IS NULL AND o.client_id IS NULL
  AND o.group_id IS NULL AND o.raw_context->>'producer' = 'buscador-midias'
  AND n.article_id = o.article_id AND n.entity_hash = o.entity_hash
  AND n.theme_id IS NULL AND n.theme_slug = o.theme_slug;
```

Depois, a conversão (`theme_slug` já veio do backfill do passo 1):

```sql
WITH ranked AS (
  SELECT c.id, c.theme_slug AS slug,
         row_number() OVER (PARTITION BY c.article_id, c.entity_hash, c.theme_slug ORDER BY c.id) AS n
  FROM midias_ia_article_contexts c
  WHERE c.theme_slug IS NOT NULL AND c.article_id BETWEEN :de AND :ate
    AND c.theme_id IS NOT NULL AND c.process_id IS NULL
    AND c.client_id IS NULL AND c.group_id IS NULL
    AND c.raw_context->>'producer' = 'buscador-midias'
), gone AS (
  DELETE FROM midias_ia_article_contexts c USING ranked r WHERE r.id = c.id AND r.n > 1
)
UPDATE midias_ia_article_contexts c
SET theme_slug = r.slug, theme_id = NULL
FROM ranked r WHERE r.id = c.id AND r.n = 1;
```

Antes de rodar: conferir a contagem esperada (linhas com tema do buscador ÷ 2) e rodar em um lote
pequeno. O `DELETE` no corpus compartilhado não tem volta; por isso o dump.

## Riscos

- **Índice único**: é o ponto mais perigoso. O índice antigo e o `ON CONFLICT` do KyX têm de mudar no
  mesmo deploy.
- **Lista de temas do KyX muda**: um `temas.id` novo sem linha em `midias_ia_theme_slugs` deixa as
  linhas de slug invisíveis para essa busca. A manutenção da tabela entra no processo de cadastrar
  um tema novo.
- **Linhas do KyX e do buscador para o mesmo artigo e tema**: com o índice novo continuam sendo
  linhas diferentes (`theme_id` preenchido contra `theme_slug` preenchido); a listagem já agrupa por
  URL da notícia e escolhe um representante (`max(c.id)`), então não aparecem duas vezes.
