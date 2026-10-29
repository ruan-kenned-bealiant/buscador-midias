"""Rodadas de busca: janela de datas, seleção de links, slot de relógio e leitura adaptativa."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.db.queue import host_of
from buscador_midias.ingestion.flow import clock_slot, day_window, is_boundary_slot, today_start

RUN_LOCK = 674931215
MAX_LINK_IDS = 100000
# Leitura adaptativa: um link é frio depois de MIN_READS leituras seguidas sem notícia nova.
MIN_READS = 4
WINDOW_DAYS = 7

DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_HOURS = [1, 6, 12, 16]
RUN_OPTIONS = {"link_ids", "since", "until", "day"}
INVALID_WINDOW = "Informe datas válidas, com fuso e início anterior ao fim."


def search_window(
    config: dict[str, Any],
    data: dict[str, Any],
    now: datetime,
    *,
    slot: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Início e fim da janela de uma rodada, a partir das opções pedidas e da configuração.

    Sem opções, vai da meia-noite local até agora (modo ``today``) ou recua ``lookback_minutes``.
    Na primeira rodada agendada do dia, ``close_previous_day`` estende o início ao dia anterior
    inteiro, para pegar o que saiu depois da última rodada.
    """
    zone = config.get("timezone", DEFAULT_TIMEZONE)
    daily = config.get("window_mode", "today") == "today"
    try:
        if "day" in data and (data.get("since") or data.get("until")):
            raise ValueError("Escolha o dia ou um período, não os dois.")
        if "day" in data:
            since, until = day_window(data["day"], now, zone)
        else:
            until = datetime.fromisoformat(data["until"]) if data.get("until") else now
            if data.get("since"):
                since = datetime.fromisoformat(data["since"])
            elif daily:
                since = today_start(until, zone)
            else:
                since = until - timedelta(minutes=config["lookback_minutes"])
        before_today = since < today_start(now, zone) or until < today_start(now, zone)
        if "day" not in data and daily and before_today:
            raise ValueError
        if (
            since.tzinfo is None
            or until.tzinfo is None
            or since >= until
            or until > now + timedelta(minutes=1)
        ):
            raise ValueError
    except (ValueError, TypeError) as error:
        message = str(error) if "day" in data and str(error) else INVALID_WINDOW
        raise CatalogError(message) from None
    first = today_start(now, zone) + timedelta(
        hours=min(config.get("schedule_hours", DEFAULT_HOURS))
    )
    closes_day = slot == first and "day" not in data and daily and config.get("close_previous_day")
    return (since - timedelta(days=1) if closes_day else since), until


_SELECT_LINKS = """
SELECT l.id, l.url, l.site_id, l.version, l.mechanism, s.version AS site_version,
 m.version AS mechanism_version, m.parameters
FROM bm_links l JOIN midias_ia_trusted_sources s ON s.id=l.site_id JOIN bm_mechanisms m ON m.model=l.mechanism
WHERE l.active=1 AND s.active AND l.deleted_at IS NULL AND s.deleted_at IS NULL
 AND NOT EXISTS (SELECT 1 FROM bm_run_links rl WHERE rl.link_id=l.id AND rl.discovery_done=false)
 AND (%(everything)s OR l.id=ANY(%(ids)s))
 AND (NOT %(daily_only)s OR l.mechanism IN ('rss','news_sitemap','g1_daily')
  OR (l.kind='feed' AND l.mechanism='auto') OR l.route='updates')
 AND (NOT %(exclude)s OR l.route IS NULL OR l.route NOT IN ('archive','review'))
ORDER BY l.id
"""

_COLD = """
WITH reads AS (
 SELECT rl.link_id, rl.state, rl.new_articles,
  row_number() OVER (PARTITION BY rl.link_id ORDER BY r.created_at DESC) AS n
 FROM bm_run_links rl JOIN bm_runs r ON r.id=rl.run_id
 WHERE r.type='search' AND rl.discovery_done AND r.created_at > now() - make_interval(days => %s)
)
SELECT link_id FROM reads WHERE n <= %s GROUP BY link_id
HAVING count(*) >= %s AND sum(new_articles) = 0 AND count(*) FILTER (WHERE state <> 'completed') = 0
"""


def cold_link_ids(db: psycopg.Connection[Any]) -> set[int]:
    """Links que as rodadas do meio do dia (nem a primeira nem a última) podem pular.

    A evidência é por leitura do link, não por rodada: quem só é lido duas vezes por dia continua
    frio em vez de oscilar. Link com leitura que falhou continua sendo lido, para se recuperar.
    """
    rows = db.execute(_COLD, (WINDOW_DAYS, MIN_READS, MIN_READS)).fetchall()
    return {row["link_id"] for row in rows}


def _link_ids(data: dict[str, Any]) -> list[int]:
    ids = data.get("link_ids", [])
    if (
        not isinstance(ids, list)
        or len(ids) > MAX_LINK_IDS
        or any(type(i) is not int or i < 1 for i in ids)
    ):
        raise CatalogError("Lista de links inválida.")
    return ids


def create_search_run(
    db: psycopg.Connection[Any],
    data: dict[str, Any],
    *,
    config: dict[str, Any],
    scheduled: bool = False,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Congela links, janela e configuração; nunca sobrepõe leituras de um mesmo link.

    A rodada agendada ocupa o slot de relógio da hora (uma só por slot) e devolve ``None`` quando
    não há o que fazer; a manual recusa com 409. A configuração e os parâmetros dos mecanismos
    ficam congelados em ``config`` para que editar o cadastro não altere uma rodada em curso.
    """
    if set(data) - RUN_OPTIONS:
        raise CatalogError("Opções da rodada desconhecidas.")
    ids = _link_ids(data)
    now = now or datetime.now(UTC)
    zone = config.get("timezone", DEFAULT_TIMEZONE)
    hours = config.get("schedule_hours", DEFAULT_HOURS)
    db.execute("SELECT pg_advisory_xact_lock(%s)", (RUN_LOCK,))
    if scheduled and not config.get("enabled"):
        return None
    slot = clock_slot(now, zone, hours) if scheduled else None
    if scheduled and (
        slot is None or db.execute("SELECT 1 FROM bm_runs WHERE slot=%s", (slot,)).fetchone()
    ):
        return None
    since, until = search_window(config, data, now, slot=slot)
    daily = bool(config.get("daily_routes_only", True))
    rows = db.execute(
        _SELECT_LINKS,
        dict(
            everything=not ids,
            ids=ids,
            daily_only=daily and (scheduled or not ids),
            exclude=daily,
        ),
    ).fetchall()
    frozen = dict(config)
    if (
        slot is not None
        and not ids
        and config.get("adaptive_polling", "off") == "enforce"
        and not is_boundary_slot(slot, now, zone, hours)
    ):
        skipped = cold_link_ids(db)
        kept = [row for row in rows if row["id"] not in skipped]
        frozen["adaptive_skipped"] = len(rows) - len(kept)
        rows = kept
    if not rows:
        if scheduled:
            return None
        raise CatalogError(
            "Nenhum link ativo disponível; os selecionados podem estar em leitura.", 409
        )
    frozen["mechanisms"] = {
        row["mechanism"]: dict(version=row["mechanism_version"], parameters=row["parameters"])
        for row in rows
    }
    ident = uuid.uuid4().hex
    run = db.execute(
        "INSERT INTO bm_runs(id,type,trigger,status,config,since,until,slot,total,started_at) "
        "VALUES(%s,'search',%s,'running',%s,%s,%s,%s,%s,now()) RETURNING *",
        (
            ident,
            "scheduled" if scheduled else "manual",
            Jsonb(frozen),
            since,
            until,
            slot,
            len(rows),
        ),
    ).fetchone()
    assert run is not None
    for link in rows:
        host = host_of(link["url"])
        db.execute(
            "INSERT INTO bm_run_links(run_id,link_id,site_id,host,link_version,site_version,"
            "mechanism_version,discovery_done,started_at) VALUES(%s,%s,%s,%s,%s,%s,%s,false,now())",
            (
                ident,
                link["id"],
                link["site_id"],
                host,
                link["version"],
                link["site_version"],
                link["mechanism_version"],
            ),
        )
        db.execute(
            "INSERT INTO bm_discovery_tasks(run_id,link_id,url,depth,slice,host) "
            "VALUES(%s,%s,%s,0,0,%s)",
            (ident, link["id"], link["url"], host),
        )
        db.execute(
            "INSERT INTO bm_link_state(link_id,last_read_at) VALUES(%s,now()) "
            "ON CONFLICT (link_id) DO UPDATE SET last_read_at=now(),updated_at=now()",
            (link["id"],),
        )
    return dict(run, links=len(rows))


# Etapas que ainda não gastaram análise paga: só elas são canceláveis.
STOPPABLE = ("dedup", "fetch", "context")


def stop_runs(db: psycopg.Connection[Any]) -> dict[str, Any]:
    """Cancela o trabalho ainda não analisado das rodadas abertas.

    Só o que ninguém executa e que vem antes da evidência e da persistência é cancelado: essas
    seguem porque a notícia já foi analisada (e paga) e concluí-las custa quase nada. As notícias
    interrompidas ficam ``cancelled``, estado que a descoberta readmite; sem isso a próxima
    rodada as trataria como já conhecidas e a notícia se perderia.
    """
    db.execute("SELECT pg_advisory_xact_lock(%s)", (RUN_LOCK,))
    articles = db.execute(
        "UPDATE bm_articles a SET state='cancelled',queued_at=NULL,owner=NULL,"
        "locked_until=NULL,updated_at=now() FROM bm_runs r WHERE r.id=a.run_id "
        "AND r.type='search' AND r.status='running' AND a.stage=ANY(%s) "
        "AND a.state IN ('pending','queued','retry') RETURNING a.stage",
        (list(STOPPABLE),),
    ).fetchall()
    tasks = db.execute(
        "UPDATE bm_discovery_tasks t SET state='cancelled',queued_at=NULL,owner=NULL,"
        "locked_until=NULL,finished_at=now(),updated_at=now() FROM bm_runs r "
        "WHERE r.id=t.run_id AND r.type='search' AND r.status='running' "
        "AND t.state IN ('pending','queued','retry') RETURNING t.stage"
    ).fetchall()
    counts: dict[str, int] = {}
    for row in [*articles, *tasks]:
        counts[row["stage"]] = counts.get(row["stage"], 0) + 1
    return dict(cancelled=len(articles) + len(tasks), by_stage=counts)
