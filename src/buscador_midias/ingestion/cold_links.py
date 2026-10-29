"""Leitura adaptativa: links que não rendem há várias leituras só são lidos na última rodada do dia.

A janela de cada rodada vai de 00h do dia até o momento da rodada, então a janela da última
rodada contém a de todas as anteriores: ler um link só nela não perde notícia, apenas a entrega
chega mais tarde. Um link é "frio" quando suas últimas ``MIN_READS`` leituras (dos últimos
``WINDOW_DAYS`` dias) não trouxeram URL candidata na janela e nenhuma descoberta falhou (link com
falha precisa continuar sendo lido para se recuperar). A evidência é por leitura do link, não
por rodada: quem só é lido uma vez por dia continua frio, em vez de oscilar. Rodadas manuais e
listas explícitas de links leem tudo, e todo link continua sendo lido todos os dias.
"""

from __future__ import annotations

from typing import Any

MIN_READS = 4
WINDOW_DAYS = 7

_COLD = (
    "WITH reads AS (SELECT p.id,p.link_id,row_number() OVER "
    "(PARTITION BY p.link_id ORDER BY p.created_at DESC) n FROM ingest_polls p "
    "WHERE p.created_at>now()-make_interval(days=>%s)) "
    "SELECT r.link_id FROM reads r "
    "LEFT JOIN ingest_jobs j ON j.poll_id=r.id AND j.stage IN ('discovery','dedup') "
    "WHERE r.n<=%s GROUP BY r.link_id "
    "HAVING count(DISTINCT r.id)>=%s "
    "AND count(*) FILTER (WHERE j.stage='dedup')=0 "
    "AND count(*) FILTER (WHERE j.stage='discovery' AND j.state<>'succeeded')=0"
)


def cold_link_ids(db: Any) -> set[int]:
    """Links que as rodadas que não são a última do dia podem pular."""
    rows = db.execute(_COLD, (WINDOW_DAYS, MIN_READS, MIN_READS)).fetchall()
    return {row["link_id"] for row in rows}
