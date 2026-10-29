"""Fila de trabalho da notícia sobre ``bm_articles``: a linha é a única verdade.

Cada função recebe uma conexão com linhas em dicionário (``Repository.connection``) e roda na
transação do chamador, para que o resultado de uma etapa, a próxima etapa e a auditoria
sejam confirmados juntos. A mensagem do RabbitMQ só leva ``(hash, etapa)``: quem manda é o banco.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from buscador_midias.db.failures import classify
from buscador_midias.db.serialize import clean
from buscador_midias.domain.url import url_hash

BATCH = 500
LEASE_SECONDS = 300
HISTORY_LIMIT = 10
MAX_ATTEMPTS_CAP = 3
BACKOFF_CAP_SECONDS = 3600


class Queue(NamedTuple):
    """Uma tabela de trabalho: as mesmas colunas de fila servem a notícias e a descobertas."""

    table: str
    key: str


ARTICLES = Queue("bm_articles", "hash")
DISCOVERY = Queue("bm_discovery_tasks", "id")

# Etapa que recebe o trabalho de cada etapa (para medir o acúmulo a jusante).
NEXT_STAGE = {"dedup": "fetch", "fetch": "context", "context": "evidence", "evidence": "persist"}
# Teto de notícias esperando na etapa seguinte antes de a etapa anterior pausar.
BACKLOG_LIMITS = {"context": 200, "persist": 200}
TERMINAL_OUTCOMES = (
    "persisted",
    "known",
    "outside_window",
    "date_unknown",
    "no_text",
    "excluded_content",
    "no_entities",
    "evidence_pending",
)


class Admission(NamedTuple):
    """O que uma leitura de documento fez com as notícias que listou."""

    listed: int
    new: int
    known: int
    reopened: int


_ADMIT = """
WITH input AS (
 SELECT * FROM jsonb_to_recordset(%(batch)s) AS x(hash text, url text, host text, payload jsonb)
), upserted AS (
 INSERT INTO bm_articles AS a (hash,url,host,site_id,link_id,document_url,first_run_id,run_id,
  payload)
 SELECT hash,url,host,%(site_id)s,%(link_id)s,%(document_url)s,%(run_id)s,%(run_id)s,payload
 FROM input
 ON CONFLICT (hash) DO UPDATE SET
  stage='dedup', state='pending', outcome=NULL, attempts=0, lane='primary',
  next_attempt_at=now(), queued_at=NULL, locked_until=NULL, owner=NULL,
  run_id=excluded.run_id, link_id=excluded.link_id, site_id=excluded.site_id,
  payload=excluded.payload, seen_count=a.seen_count+1, last_seen_at=now(), updated_at=now()
 WHERE (
  a.state='done' AND a.outcome='outside_window' AND (
   (a.payload->>'published_at')::timestamptz BETWEEN %(since)s AND %(until)s
   OR ((a.payload->>'route_period_since')::timestamptz <= %(until)s
       AND (a.payload->>'route_period_until')::timestamptz >= %(since)s))
 ) OR (a.state='done' AND a.outcome='date_unknown' AND %(allow_undated)s)
 OR a.state='cancelled'
 RETURNING hash, (xmax = 0) AS inserted
), seen AS (
 UPDATE bm_articles a SET seen_count=a.seen_count+1, last_seen_at=now()
 FROM input i WHERE a.hash=i.hash AND NOT EXISTS (SELECT 1 FROM upserted u WHERE u.hash=a.hash)
 RETURNING a.hash
)
SELECT (SELECT count(*) FROM upserted WHERE inserted) AS new,
       (SELECT count(*) FROM upserted WHERE NOT inserted) AS reopened,
       (SELECT count(*) FROM seen) AS known
"""


def host_of(url: str) -> str:
    """Host em minúsculas, que é a unidade de educação e de justiça entre sites."""
    return (urlsplit(url).hostname or "").lower()


def admit(
    db: psycopg.Connection[Any],
    candidates: list[dict[str, Any]],
    *,
    run: dict[str, Any],
    link: dict[str, Any],
    document_url: str,
) -> Admission:
    """Admite as URLs de um documento: novas entram, conhecidas só contam a revisita.

    Reabre apenas o que a regra de janela manda: ``outside_window`` cuja data (ou período de rota
    aprendido) agora cai na janela, ``date_unknown`` com ``allow_undated`` e ``cancelled``.
    Concluídas, em andamento e falhas não mudam aqui (falha só reabre por ``maintain``/``retry``).
    """
    unique = {url_hash(c["url"]): c for c in candidates}
    totals = [0, 0, 0]
    for start in range(0, len(unique), BATCH):
        part = list(unique.items())[start : start + BATCH]
        batch = [
            dict(hash=h, url=c["url"], host=host_of(c["url"]), payload=clean(c)) for h, c in part
        ]
        row = db.execute(
            _ADMIT,
            dict(
                batch=Jsonb(batch),
                site_id=link["site_id"],
                link_id=link["id"],
                document_url=document_url,
                run_id=run["id"],
                since=run["since"],
                until=run["until"],
                allow_undated=bool(run["config"].get("allow_undated", False)),
            ),
        ).fetchone()
        assert row is not None
        totals[0] += row["new"]
        totals[1] += row["reopened"]
        totals[2] += row["known"]
    new, reopened, known = totals
    db.execute(
        "UPDATE bm_run_links SET listed=listed+%s, new_articles=new_articles+%s,"
        "known_articles=known_articles+%s WHERE run_id=%s AND link_id=%s",
        (len(unique), new + reopened, known, run["id"], link["id"]),
    )
    return Admission(len(unique), new, known, reopened)


# --- reservar, concluir e falhar uma etapa ------------------------------------------------------


def claim(
    db: psycopg.Connection[Any],
    stage: str,
    keys: list[Any],
    owner: str,
    *,
    lane: str | None = None,
    lease_seconds: int = LEASE_SECONDS,
    queue: Queue = ARTICLES,
) -> list[dict[str, Any]]:
    """Reserva o que ainda espera esta etapa; mensagem repetida ou velha não reserva.

    A reserva é um compare-and-set no estado (``pending``, ``queued`` ou ``retry``) com a hora de
    nova tentativa vencida, então dois consumidores nunca executam a mesma etapa ao mesmo tempo.
    Devolve as linhas no formato de "tarefa" que o motor entende: ``id`` é a chave da linha e
    ``run`` traz a rodada com a configuração congelada.
    """
    if not keys:
        return []
    rows = db.execute(
        f"WITH locked AS (SELECT {queue.key} AS k FROM {queue.table} "
        f"WHERE {queue.key}=ANY(%(keys)s) AND stage=%(stage)s "
        "AND state IN ('pending','queued','retry') AND next_attempt_at<=now() "
        "AND (%(lane)s::text IS NULL OR lane=%(lane)s) ORDER BY 1 FOR UPDATE) "
        f"UPDATE {queue.table} a SET state='running',owner=%(owner)s,attempts=a.attempts+1,"
        "locked_until=now()+%(lease)s*interval '1 second',queued_at=NULL,updated_at=now() "
        f"FROM locked l WHERE a.{queue.key}=l.k RETURNING a.*",
        dict(keys=keys, stage=stage, lane=lane, owner=owner, lease=lease_seconds),
    ).fetchall()
    if not rows:
        return []
    runs = {
        r["id"]: r
        for r in db.execute(
            "SELECT * FROM bm_runs WHERE id=ANY(%s)", (list({r["run_id"] for r in rows}),)
        ).fetchall()
    }
    return [
        dict(row, id=row[queue.key], subject=row[queue.key], run=runs[row["run_id"]])
        for row in rows
    ]


def _add_numbers(total: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    """Soma só os números (nunca booleanos) de ``metrics`` em ``total``."""
    merged = dict(total)
    for key, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        merged[key] = merged.get(key, 0) + value
    return merged


def entry(stage: str, attempt: int, outcome: str, detail: str = "") -> dict[str, Any]:
    """Uma linha do histórico curto de tentativas guardado na própria tarefa."""
    item = dict(stage=stage, attempt=attempt, outcome=outcome, at=datetime.now(UTC).isoformat())
    if detail:
        item["detail"] = detail[:200]
    return item


def locked(
    db: psycopg.Connection[Any], job: dict[str, Any], queue: Queue = ARTICLES
) -> dict[str, Any] | None:
    """A linha da tarefa, travada, se ela ainda pertence a este consumidor e a esta tentativa."""
    return db.execute(
        f"SELECT * FROM {queue.table} WHERE {queue.key}=%s AND state='running' AND owner=%s "
        "AND stage=%s AND attempts=%s FOR UPDATE",
        (job[queue.key], job["owner"], job["stage"], job["attempts"]),
    ).fetchone()


_GUARDED = (
    "SELECT a.* FROM bm_articles a JOIN unnest(%(hashes)s::text[],%(owners)s::text[],"
    "%(stages)s::text[],%(attempts)s::int[]) AS j(hash,owner,stage,attempts) "
    "ON a.hash=j.hash AND a.owner=j.owner AND a.stage=j.stage AND a.attempts=j.attempts "
    "WHERE a.state='running' ORDER BY a.hash FOR UPDATE OF a"
)

_APPLY = (
    "UPDATE bm_articles a SET stage=COALESCE(x.nxt,a.stage),"
    "state=CASE WHEN x.nxt IS NULL THEN 'done' ELSE 'pending' END,outcome=x.status,"
    "attempts=x.attempts,lane='primary',owner=NULL,locked_until=NULL,queued_at=NULL,"
    "next_attempt_at=CASE WHEN x.nxt IS NULL THEN a.next_attempt_at ELSE now() END,"
    "payload=a.payload||x.patch,cost=x.cost,history=x.history,"
    "corpus_article_id=COALESCE(x.article_id,a.corpus_article_id),"
    "failed_stage=NULL,reason=NULL,reason_class=NULL,retryable=NULL,failed_at=NULL,"
    "detail=NULL,updated_at=now() FROM jsonb_to_recordset(%(batch)s) AS x(hash text,nxt text,"
    "status text,attempts int,patch jsonb,cost jsonb,history jsonb,article_id bigint) "
    "WHERE a.hash=x.hash"
)


def complete_many(
    db: psycopg.Connection[Any], jobs: list[dict[str, Any]], outputs: list[dict[str, Any]]
) -> int:
    """Confirma o resultado de várias notícias de uma vez (um ``SELECT`` e um ``UPDATE``).

    Cada saída avança a notícia (``next``) ou a encerra (``status`` terminal). As que perderam a
    reserva são ignoradas; devolve quantas foram aplicadas. Uma saída inválida rejeita o lote.
    """
    if len(jobs) != len(outputs):
        raise ValueError("Quantidade de resultados diferente da quantidade de tarefas")
    for output in outputs:
        if not output.get("next") and output.get("status") not in TERMINAL_OUTCOMES:
            raise ValueError("A etapa deve devolver 'next' ou um 'status' terminal conhecido.")
    if not jobs:
        return 0
    rows = {
        r["hash"]: r
        for r in db.execute(
            _GUARDED,
            dict(
                hashes=[j["hash"] for j in jobs],
                owners=[j["owner"] for j in jobs],
                stages=[j["stage"] for j in jobs],
                attempts=[j["attempts"] for j in jobs],
            ),
        ).fetchall()
    }
    batch = []
    for job, output in zip(jobs, outputs, strict=True):
        row = rows.get(job["hash"])
        if row is None:
            continue
        stage, nxt = job["stage"], output.get("next")
        cost = {**row["cost"]}
        added = _add_numbers(cost.get(stage, {}), output.get("metrics") or {})
        if added:
            cost[stage] = added
        history = [*row["history"], entry(stage, job["attempts"], "ok")][-HISTORY_LIMIT:]
        batch.append(
            dict(
                hash=job["hash"],
                nxt=nxt,
                status=None if nxt else output["status"],
                attempts=0 if nxt else row["attempts"],
                patch=clean(output.get("article") or {}),
                cost=clean(cost),
                history=history,
                article_id=output.get("article_id"),
            )
        )
    if batch:
        db.execute(_APPLY, dict(batch=Jsonb(batch)))
    return len(batch)


def complete(db: psycopg.Connection[Any], job: dict[str, Any], output: dict[str, Any]) -> bool:
    """Confirma o resultado de uma etapa: aplica o patch e avança ou encerra a notícia.

    ``output["next"]`` leva à próxima etapa (``pending``, tentativas zeradas); ``output["status"]``
    é um desfecho terminal. Devolve ``False`` se a reserva foi substituída (consumidor antigo).
    """
    return complete_many(db, [job], [output]) == 1


def renew_lease(
    db: psycopg.Connection[Any],
    job: dict[str, Any],
    *,
    seconds: int = LEASE_SECONDS,
    queue: Queue = ARTICLES,
) -> bool:
    """Estende a reserva de quem ainda é o dono da tentativa; ``False`` se ela foi substituída.

    Etapas longas (evidência, persistência, contexto) chamam isto de tempos em tempos para não
    perder a reserva e serem reexecutadas por ``maintain``.
    """
    return bool(
        db.execute(
            f"UPDATE {queue.table} SET locked_until=now()+%s*interval '1 second',"
            f"updated_at=now() WHERE {queue.key}=%s AND state='running' AND owner=%s "
            "AND stage=%s AND attempts=%s",
            (seconds, job[queue.key], job["owner"], job["stage"], job["attempts"]),
        ).rowcount
    )


def fail(
    db: psycopg.Connection[Any],
    job: dict[str, Any],
    reason: str,
    *,
    busy: bool = False,
    metrics: dict[str, Any] | None = None,
    defer_seconds: int = 5,
    recovery: bool = False,
    retryable: bool = True,
    queue: Queue = ARTICLES,
) -> str:
    """Espera crescente e limite de tentativas; contenção de host não reprova a tarefa.

    Devolve ``"retry"``, ``"failed"`` ou ``"stale"`` (a reserva já não é deste consumidor).
    """
    row = locked(db, job, queue)
    if row is None:
        return "stale"
    reason = clean(reason)
    config = job["run"]["config"]
    limit = min(MAX_ATTEMPTS_CAP, int(config.get("max_attempts", MAX_ATTEMPTS_CAP)))
    failure = classify(reason, retryable=retryable)
    attempts = row["attempts"] - (1 if busy else 0)
    immediate = not retryable or failure.klass in ("permanent", "blocked")
    exhausted = not busy and (immediate or attempts >= limit)
    delay = defer_seconds if busy else _backoff(attempts, int(config.get("retry_minutes", 1)))
    after = (metrics or {}).get("retry_after_seconds")
    host = (metrics or {}).get("retry_after_host")
    if isinstance(after, int) and 0 <= after <= 2**31 - 1:
        delay = max(delay, after)
        if isinstance(host, str) and 0 < len(host) <= 253:
            db.execute(
                "INSERT INTO bm_host_state(host,cooldown_until) "
                "VALUES(%s,now()+%s*interval '1 second') ON CONFLICT (host) DO UPDATE SET "
                "cooldown_until=GREATEST(bm_host_state.cooldown_until,excluded.cooldown_until)",
                (host, after),
            )
    outcome = "failed" if exhausted else "retry"
    history = [*row["history"], entry(job["stage"], row["attempts"], outcome, reason)]
    db.execute(
        f"UPDATE {queue.table} SET state=%(state)s,attempts=%(attempts)s,"
        "lane=CASE WHEN %(recovery)s THEN 'recovery' ELSE lane END,"
        "next_attempt_at=now()+%(delay)s*interval '1 second',owner=NULL,locked_until=NULL,"
        "queued_at=NULL,history=%(history)s,"
        "failed_stage=CASE WHEN %(exhausted)s THEN stage END,"
        "reason=CASE WHEN %(exhausted)s THEN %(code)s END,"
        "reason_class=CASE WHEN %(exhausted)s THEN %(klass)s END,"
        "retryable=CASE WHEN %(exhausted)s THEN %(retryable)s END,"
        "failed_at=CASE WHEN %(exhausted)s THEN now() END,"
        "detail=CASE WHEN %(exhausted)s THEN %(detail)s END,updated_at=now() "
        f"WHERE {queue.key}=%(key)s",
        dict(
            state=outcome,
            attempts=attempts,
            recovery=recovery,
            delay=delay,
            history=Jsonb(history[-HISTORY_LIMIT:]),
            exhausted=exhausted,
            code=failure.code,
            klass=failure.klass,
            retryable=retryable and failure.klass == "transient",
            detail=failure.detail,
            key=job[queue.key],
        ),
    )
    return outcome


def _backoff(attempts: int, retry_minutes: int) -> int:
    """Espera antes da próxima tentativa: dobra a cada falha, com teto de uma hora."""
    return int(min(BACKOFF_CAP_SECONDS, 60 * retry_minutes * 2 ** max(0, attempts - 1)))


# --- manutenção, retry e publicação -------------------------------------------------------------


class Maintenance(NamedTuple):
    """O que uma passada de manutenção consertou."""

    expired: int
    republished: int
    auto_retried: int


def _expired_sql(queue: Queue) -> str:
    return f"""
WITH gone AS (
 SELECT a.{queue.key} AS k, a.stage, a.attempts, a.history,
  a.attempts >= LEAST(%(cap)s, COALESCE((r.config->>'max_attempts')::int, %(cap)s)) AS exhausted
 FROM {queue.table} a JOIN bm_runs r ON r.id=a.run_id
 WHERE a.state='running' AND a.locked_until < now() FOR UPDATE OF a SKIP LOCKED
)
UPDATE {queue.table} a SET
 state=CASE WHEN g.exhausted THEN 'failed' ELSE 'retry' END,
 lane=CASE WHEN g.exhausted THEN a.lane ELSE 'recovery' END,
 next_attempt_at=now(), owner=NULL, locked_until=NULL, queued_at=NULL,
 failed_stage=CASE WHEN g.exhausted THEN a.stage END,
 reason=CASE WHEN g.exhausted THEN 'lease_expired' END,
 reason_class=CASE WHEN g.exhausted THEN 'transient' END,
 retryable=CASE WHEN g.exhausted THEN true END,
 failed_at=CASE WHEN g.exhausted THEN now() END,
 detail=CASE WHEN g.exhausted THEN 'Reserva expirou sem resposta do consumidor' END,
 history=(g.history || jsonb_build_array(jsonb_build_object('stage',a.stage,'attempt',a.attempts,
  'outcome','lease_expired','at',now())))::jsonb,
 updated_at=now()
FROM gone g WHERE a.{queue.key}=g.k
"""


_AUTO_RETRY = """
UPDATE bm_articles a SET state='retry', stage=COALESCE(a.failed_stage,a.stage), attempts=0,
 passes=a.passes+1, lane='primary', next_attempt_at=now(), queued_at=NULL, owner=NULL,
 locked_until=NULL, updated_at=now()
FROM bm_runs r WHERE r.id=a.run_id AND r.status<>'running' AND a.state='failed'
 AND a.reason_class='transient'
 AND a.retryable AND a.passes < COALESCE((r.config->>'failed_retry_limit')::int, 1)
 AND a.failed_at < now() - %(after)s * interval '1 second'
"""


def maintain(
    db: psycopg.Connection[Any],
    *,
    queued_stale_seconds: int = 900,
    auto_retry_after_seconds: int = 3600,
    queue: Queue = ARTICLES,
) -> Maintenance:
    """Recupera reservas expiradas, republica mensagens perdidas e retenta falhas transitórias.

    O retry automático vale só para notícias e só depois que a rodada que falhou terminou.
    """
    expired = db.execute(_expired_sql(queue), dict(cap=MAX_ATTEMPTS_CAP)).rowcount
    republished = db.execute(
        f"UPDATE {queue.table} SET state='pending',queued_at=NULL,updated_at=now() "
        "WHERE state='queued' AND queued_at < now() - %s * interval '1 second'",
        (queued_stale_seconds,),
    ).rowcount
    retried = (
        db.execute(_AUTO_RETRY, dict(after=auto_retry_after_seconds)).rowcount
        if queue is ARTICLES
        else 0
    )
    return Maintenance(expired, republished, retried)


def retry_failed(
    db: psycopg.Connection[Any],
    *,
    stage: str | None = None,
    reason: str | None = None,
    reason_class: str | None = None,
    run_id: str | None = None,
    site_id: int | None = None,
    hashes: list[str] | None = None,
    limit: int | None = None,
) -> int:
    """Reabre as falhas que o filtro escolhe, por decisão de uma pessoa (sem limite de passadas)."""
    return db.execute(
        "WITH picked AS (SELECT hash FROM bm_articles WHERE state='failed' "
        "AND (%(stage)s::text IS NULL OR failed_stage=%(stage)s) "
        "AND (%(reason)s::text IS NULL OR reason=%(reason)s) "
        "AND (%(klass)s::text IS NULL OR reason_class=%(klass)s) "
        "AND (%(run)s::text IS NULL OR run_id=%(run)s) "
        "AND (%(site)s::bigint IS NULL OR site_id=%(site)s) "
        "AND (%(hashes)s::text[] IS NULL OR hash=ANY(%(hashes)s)) "
        "ORDER BY failed_at, hash LIMIT %(limit)s FOR UPDATE SKIP LOCKED) "
        "UPDATE bm_articles a SET state='retry', stage=COALESCE(a.failed_stage,a.stage),"
        "attempts=0, passes=a.passes+1, lane='primary', next_attempt_at=now(), queued_at=NULL,"
        "owner=NULL, locked_until=NULL, updated_at=now() FROM picked p WHERE a.hash=p.hash",
        dict(
            stage=stage,
            reason=reason,
            klass=reason_class,
            run=run_id,
            site=site_id,
            hashes=hashes,
            limit=limit,
        ),
    ).rowcount


def capacity(db: psycopg.Connection[Any], stage: str, limits: dict[str, int] | None = None) -> bool:
    """``False`` quando a etapa seguinte já acumulou notícias demais: a etapa pausa e espera."""
    limits = BACKLOG_LIMITS if limits is None else limits
    downstream = NEXT_STAGE.get(stage)
    if downstream not in limits:
        return True
    row = db.execute(
        "SELECT count(*) AS n FROM bm_articles WHERE stage=%s AND lane='primary' "
        "AND state IN ('pending','queued','running','retry')",
        (downstream,),
    ).fetchone()
    assert row is not None
    return bool(row["n"] < limits[downstream])


def _publish_sql(queue: Queue) -> str:
    """Escolha por host sem varrer a fila: lista os hosts pelo índice e busca só as vagas dele.

    Os hosts distintos saem de um *skip-scan* recursivo em ``(stage, lane, host)``; para cada um
    conta-se, até o teto, quantos esperam (isso define as vagas extras) e quantos já estão em voo,
    e só então se buscam as notícias mais antigas que cabem. O custo cresce com o número de hosts
    e de vagas, não com o tamanho da fila (500 mil notícias esperando custavam mais de 2 s com
    uma função de janela sobre todas elas).
    """
    waiting = (
        "t.stage=%(stage)s AND t.lane=%(lane)s AND t.state IN ('pending','retry') "
        "AND t.next_attempt_at <= now()"
    )
    return f"""
WITH RECURSIVE hosts AS (
 (SELECT t.host FROM {queue.table} t WHERE t.stage=%(stage)s AND t.lane=%(lane)s
   AND t.state IN ('pending','retry') ORDER BY t.host LIMIT 1)
 UNION ALL
 SELECT (SELECT t.host FROM {queue.table} t WHERE t.stage=%(stage)s AND t.lane=%(lane)s
   AND t.state IN ('pending','retry') AND t.host > h.host ORDER BY t.host LIMIT 1)
 FROM hosts h WHERE h.host IS NOT NULL
), per_host AS (
 SELECT h.host,
  CASE WHEN %(per_host)s::int IS NULL THEN %(limit)s::int ELSE
   LEAST(COALESCE(%(ceiling)s::int, %(per_host)s::int),
    %(per_host)s::int + (SELECT count(*) FROM (SELECT 1 FROM {queue.table} t
      WHERE {waiting} AND t.host=h.host LIMIT %(cap)s::int) c) / %(step)s::int)
   - (SELECT count(*) FROM {queue.table} t WHERE t.stage=%(stage)s AND t.host=h.host
      AND t.state IN ('queued','running')) END AS slots
 FROM hosts h LEFT JOIN bm_host_state hs ON hs.host=h.host
 WHERE h.host IS NOT NULL AND (hs.cooldown_until IS NULL OR hs.cooldown_until <= now())
), picked AS (
 SELECT x.k, x.rn, x.next_attempt_at FROM per_host p CROSS JOIN LATERAL (
  SELECT t.{queue.key} AS k, t.next_attempt_at,
   row_number() OVER (ORDER BY t.next_attempt_at, t.{queue.key}) AS rn
  FROM {queue.table} t WHERE {waiting} AND t.host=p.host
  ORDER BY t.next_attempt_at, t.{queue.key} LIMIT GREATEST(p.slots, 0)
 ) x
), chosen AS (
 SELECT k, rn, next_attempt_at FROM picked ORDER BY rn, next_attempt_at, k LIMIT %(limit)s
), locked AS (
 SELECT a.{queue.key} AS k FROM {queue.table} a JOIN chosen c ON c.k=a.{queue.key}
 WHERE a.state IN ('pending','retry') FOR UPDATE OF a SKIP LOCKED
)
UPDATE {queue.table} a SET state='queued', queued_at=now(), updated_at=now()
FROM locked l, chosen c WHERE a.{queue.key}=l.k AND c.k=a.{queue.key}
RETURNING a.{queue.key} AS k, c.rn, c.next_attempt_at
"""


def reserve_for_publish(
    db: psycopg.Connection[Any],
    stage: str,
    lane: str,
    *,
    limit: int,
    per_host: int | None = None,
    per_host_max: int | None = None,
    backlog_step: int = 100,
    queue: Queue = ARTICLES,
) -> list[Any]:
    """Escolhe quem vai para a fila: os mais antigos, revezando entre hosts.

    ``per_host`` limita quantas tarefas de um mesmo host ficam em voo (``queued`` + ``running``).
    Com ``per_host_max``, um host com fila longa ganha uma vaga extra a cada ``backlog_step``
    notícias esperando, até esse teto. Hosts em espera por ``Retry-After`` ficam de fora.
    A escolha é do publicador, não do consumidor: um site enorme não ocupa a frente da fila e
    nenhum worker gira em ``HostBusy``.
    """
    if limit <= 0:
        return []
    rows = db.execute(
        _publish_sql(queue),
        dict(
            stage=stage,
            lane=lane,
            limit=limit,
            per_host=per_host,
            ceiling=per_host_max,
            step=max(1, backlog_step),
            cap=max(
                0, ((per_host_max or per_host or 1) - (per_host or 1) + 1) * max(1, backlog_step)
            ),
        ),
    ).fetchall()
    return [r["k"] for r in sorted(rows, key=lambda r: (r["rn"], r["next_attempt_at"], r["k"]))]
