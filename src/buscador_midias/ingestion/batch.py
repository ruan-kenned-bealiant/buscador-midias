"""Finalização relacional de lotes de dedup, com reserva e outbox por URL."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

# Todas as mutações dependem das reservas ainda válidas. Reentregas não criam nova etapa.
SQL = """
WITH input AS MATERIALIZED (
 SELECT * FROM jsonb_to_recordset(%s) AS x(
 id text,owner text,generation integer,attempt integer,dispatch integer,
 patch jsonb,status text,article_id bigint,result jsonb,outcome text,
 next_stage text,next_id text,next_payload jsonb)
), reserved AS MATERIALIZED (
 SELECT j.*,i.patch,i.status article_status,i.article_id corpus_id,i.result summary,
 i.outcome result_outcome,i.next_stage,i.next_id,i.next_payload
 FROM input i JOIN ingest_jobs j ON j.id=i.id
 JOIN ingest_articles a ON a.hash=j.hash AND a.generation=j.generation
 WHERE j.state='running' AND j.owner=i.owner AND j.generation=i.generation
 AND j.attempts=i.attempt AND j.dispatch=i.dispatch
 ORDER BY j.id FOR UPDATE OF j
), articles AS (
 UPDATE ingest_articles a SET payload=a.payload||r.patch,status=r.article_status,
 article_id=COALESCE(r.corpus_id,a.article_id),updated_at=clock_timestamp()
 FROM reserved r WHERE a.hash=r.hash AND a.generation=r.generation RETURNING a.hash
), tasks AS (
 INSERT INTO ingest_jobs(id,stage,subject,hash,run_id,poll_id,generation,payload)
 SELECT r.next_id,r.next_stage,r.hash,r.hash,r.run_id,r.poll_id,r.generation,r.next_payload
 FROM reserved r JOIN articles a ON a.hash=r.hash WHERE r.next_stage IS NOT NULL
 ON CONFLICT DO NOTHING RETURNING id,stage
), published AS (
 INSERT INTO ingest_outbox(job_id,dispatch,stage) SELECT id,1,stage FROM tasks
), attempts AS (
 UPDATE ingest_attempts a SET finished_at=clock_timestamp(),outcome='succeeded',
 metrics=COALESCE(r.summary->'metrics','{}'::jsonb)
 FROM reserved r WHERE a.job_id=r.id AND a.attempt=r.attempts
), finished AS (
 UPDATE ingest_jobs j SET state='succeeded',outcome=r.result_outcome,result=r.summary,
 finished_at=clock_timestamp(),lease_until=NULL,updated_at=clock_timestamp()
 FROM reserved r WHERE j.id=r.id RETURNING j.id
) SELECT count(*) completed FROM finished
"""


def finish_dedup(db: Any, jobs: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> int:
    """Aplica um lote inteiro em uma instrução, sem modificar reservas substituídas."""
    rows = []
    for job, output in zip(jobs, outputs, strict=True):
        summary = {
            k: v for k, v in output.items() if k not in ("candidates", "children", "article")
        }
        summary.update(candidates=0, children=0)
        rows.append(
            dict(
                id=job["id"],
                owner=job["owner"],
                generation=job["generation"],
                attempt=job["attempts"],
                dispatch=job["dispatch"],
                patch=output.get("article", {}),
                status=output.get("status", output.get("next", "completed")),
                article_id=output.get("article_id"),
                result=summary,
                outcome=output.get("outcome", "ok"),
                next_stage=output.get("next"),
                next_id=uuid.uuid4().hex,
                next_payload={
                    k: v
                    for k, v in job["payload"].items()
                    if k in ("retry_config", "retry_config_version")
                },
            )
        )
    if not rows:
        return 0
    return int(db.execute(SQL, (Jsonb(rows),)).fetchone()["completed"])
