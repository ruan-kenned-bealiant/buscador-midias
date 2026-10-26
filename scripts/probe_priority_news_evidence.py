"""Prova finita de descoberta, texto e evidência; não altera catálogo ou corpus."""

# ruff: noqa: E501 -- HTML do relatório preservado em literais completos.
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from buscador_midias.catalog.http_transport import make_reader
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.postgres import connect_postgres
from buscador_midias.catalog.testing import run_test
from buscador_midias.evidence.capture import EvidenceCapturer
from buscador_midias.evidence.storage import LocalEvidenceStore
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.state.store import FindingUrl
from map_editorial_priority import credentials, write
from map_priority_sources import DISTRIBUTION, SEEDS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/gerenciador/teste-noticias-evidencias-20261026"
ZONE = ZoneInfo("America/Sao_Paulo")
LOCK = threading.Lock()
HOST_LOCKS = {}
MAX_DISK = 180 * 1024 * 1024
USED = 0
EXCLUDED = re.compile(
    r"/(?:plus|advertising|creativegroup|patrocinado|sponsored|branded|cooking|games|weather|podcasts?|videos?|audio)(?:/|$)|previsao-do-tempo|previsao-tempo",
    re.I,
)


def editorial(url):
    """Não usa publicidade e páginas de serviço como prova de notícia editorial."""
    return not bool(EXCLUDED.search(url))


def publication_day(value, now):
    """Não transforma data ausente ou futura em comprovação de publicação hoje."""
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None or date > now:
            return "desconhecida"
        return "hoje" if date.astimezone(ZONE).date() == now.astimezone(ZONE).date() else "anterior"
    except (ValueError, TypeError):
        return "desconhecida"


def freeze():
    """Congela revisões reais com transação PostgreSQL estritamente de leitura."""
    ids = [
        s["source_id"]
        for s in json.loads(
            (
                ROOT
                / "var/gerenciador/recuperacao-transportes-20261026/cobertura-prioritarios.json"
            ).read_text()
        )
    ]
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET TRANSACTION READ ONLY")
        sources = db.execute(
            "SELECT * FROM midias_manager.sites WHERE id=ANY(%s) ORDER BY id", (ids,)
        ).fetchall()
        links = db.execute(
            "SELECT * FROM midias_manager.links WHERE source_id=ANY(%s)", (ids,)
        ).fetchall()
        models = db.execute("SELECT * FROM midias_manager.mechanisms").fetchall()
    return dict(
        started_at=datetime.now(UTC).isoformat(),
        sources=sources,
        links=links,
        models=models,
        limits=dict(
            workers=8,
            tests_per_source=3,
            seconds_per_test=35,
            articles_per_test=3,
            evidence_per_source=2,
            max_evidence_bytes=MAX_DISK,
        ),
        writes_catalog=False,
        writes_corpus=False,
        bright_data=False,
        openrouter=False,
    )


class RecordingReader:
    """Retém apenas respostas HTML para reutilização na captura, sem novo download."""

    def __init__(self, roots, progress, transport):
        """Cria o leitor com orçamento independente por mecanismo."""
        self.inner = make_reader(
            roots, progress, transport=transport, seconds=35, requests=24, max_bytes=4 * 1024 * 1024
        )
        self.pages = {}

    @property
    def requests(self):
        """Expõe a contagem real da pilha HTTP."""
        return self.inner.requests

    def get(self, url):
        """Obtém a resposta e retém o HTML integral para evidência."""
        response = self.inner.get(url)
        if response.ok and (
            "html" in (response.content_type or "").lower()
            or response.content.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        ):
            self.pages[url] = response
        return response

    def close(self):
        """Fecha os recursos de transporte quando a implementação exige."""
        close = getattr(self.inner, "close", None)
        if close:
            close()


def rank(link, now):
    """Prioriza rotas comprovadas com amostras editoriais recentes."""
    test = link.get("last_test") or {}
    samples = [s for s in test.get("samples", []) if editorial(s.get("url", ""))]
    return (
        not bool(link.get("active") and not link.get("deleted_at")),
        not any(publication_day(s.get("published_at"), now) == "hoje" for s in samples),
        not bool(test.get("usable_for_news")),
        link["kind"] != "feed",
        len(link["url"]),
    )


def test_source(source, manifest):
    """Testa até três rotas e grava até duas evidências validadas da fonte."""
    global USED
    now = datetime.fromisoformat(manifest["started_at"])
    links = sorted(
        [
            link
            for link in manifest["links"]
            if link["source_id"] == source["id"] and editorial(link["url"])
        ],
        key=lambda link: rank(link, now),
    )
    # Limita repetição e evita dois candidatos que só diferem em HTTP/HTTPS.
    candidates, seen = [], set()
    for link in links:
        key = re.sub(r"^https?://", "", link["url"]).rstrip("/")
        if key not in seen:
            candidates.append(link)
            seen.add(key)
    if not any(link.get("active") and not link.get("deleted_at") for link in candidates):
        seeds = [
            dict(
                id=None,
                source_id=source["id"],
                url=u,
                model="auto",
                kind="feed" if "rss" in u or "feed" in u else "sitemap",
                access_transport="curl_chrome_h2",
            )
            for u in dict.fromkeys(DISTRIBUTION.get(source["id"], []) + SEEDS.get(source["id"], []))
            if editorial(u)
        ]
        candidates = seeds + candidates
    if not candidates:
        # Endereços convencionais são candidatos novos, não comprovação ou cadastro automático.
        official = {
            374: [
                "https://feeds.washingtonpost.com/rss/world",
                "https://www.washingtonpost.com/sitemaps/news-sitemap.xml",
            ],
            381: [
                "https://rssfeeds.usatoday.com/usatoday-NewsTopStories",
                "https://www.usatoday.com/sitemap/",
            ],
        }
        addresses = official.get(
            source["id"],
            [
                source["url"].rstrip("/") + suffix
                for suffix in ("/sitemap.xml", "/rss.xml", "/feed/")
            ],
        )
        candidates = [
            dict(
                id=None,
                url=url,
                kind="feed" if "rss" in url or "feed" in url else "sitemap",
                model="auto",
                access_transport="curl_chrome_h2",
                origin="bounded_conventional_probe",
            )
            for url in addresses
        ]
    # A mesma URL não é repetida quando está tanto no catálogo quanto nas sementes oficiais.
    candidates = list({link["url"]: link for link in reversed(candidates)}.values())[::-1]
    models = {m["model"]: m for m in manifest["models"]}
    result = dict(
        source_id=source["id"],
        name=source["name"],
        source_url=source["url"],
        tests=[],
        evidence=[],
        excluded=[],
        started_at=datetime.now(UTC).isoformat(),
    )
    started = time.monotonic()
    for link in candidates[:3]:
        if (
            sum(e["status"] == "captured" for e in result["evidence"]) >= 2
            or time.monotonic() - started > 115
        ):
            break
        saved = models.get(link.get("model"), {})
        mechanism = get_mechanism(link.get("model") or "auto", link["kind"])
        parameters = {
            p.key: (saved.get("parameters") or {}).get(p.key, p.default)
            for p in mechanism.parameters
        }
        parameters.update(
            lookback_days=1,
            timeout_seconds=35,
            max_requests=24,
            article_samples=3,
            sample_order="first",
            max_response_mib=4,
        )
        if "max_documents" in parameters:
            parameters.update(max_documents=4, max_depth=10)
        config = dict(
            test_id=uuid.uuid4().hex,
            url=link["url"],
            kind=link["kind"],
            model=mechanism.id,
            parameters=parameters,
            access_transport=link.get("access_transport") or "direct",
            bright_data=False,
            window_until=manifest["started_at"],
        )
        reader = RecordingReader(
            [source["url"], link["url"]], lambda _: None, config["access_transport"]
        )
        try:
            tested = run_test(
                config, source, lambda _: None, factory=lambda _r, _p, reader=reader: reader
            )
            tested["link_id"] = link.get("id")
            result["tests"].append(tested)
            for sample in tested["samples"]:
                if not sample.get("text") or not sample.get("article"):
                    continue
                if not editorial(sample["url"]):
                    result["excluded"].append(sample)
                    continue
                response = reader.pages.get(sample["url"])
                if not response or sum(e["status"] == "captured" for e in result["evidence"]) >= 2:
                    continue
                digest = hashlib.sha256(sample["url"].encode()).hexdigest()
                if any(e["url_hash"] == digest for e in result["evidence"]):
                    continue
                with LOCK:
                    if (
                        USED + len(response.content) + 1024 > MAX_DISK
                        or shutil.disk_usage(OUT).free < 300 * 1024 * 1024
                    ):
                        result["storage_limit"] = True
                        continue
                    USED += len(response.content) + 1024
                    record = EvidenceCapturer(
                        LocalEvidenceStore(OUT / "evidence"),
                        bright_data=None,
                        reuse=True,
                        now=lambda: now,
                    ).capture(
                        FindingUrl(digest, sample["url"], sample["title"], str(source["id"]), ()),
                        PageFetch(
                            sample["url"],
                            PageStatus.OK,
                            response.status,
                            response.content,
                            response.content_type,
                            response.final_url,
                        ),
                        expected_headline=sample["title"],
                    )
                entry = dict(
                    **asdict(record),
                    url=sample["url"],
                    title=sample["title"],
                    published_at=sample.get("published_at"),
                    publication=publication_day(sample.get("published_at"), now),
                    link_url=link["url"],
                    transport=config["access_transport"],
                )
                if record.status == "captured":
                    data = (OUT / "evidence" / record.path).read_bytes()
                    if hashlib.sha256(data).hexdigest() != record.sha256:
                        raise ValueError("Evidência gravada não confere com SHA-256")
                result["evidence"].append(entry)
        except Exception as exc:
            result["tests"].append(
                dict(url=link["url"], outcome="exception", error=f"{type(exc).__name__}: {exc}")
            )
        finally:
            reader.close()
    result["duration_seconds"] = round(time.monotonic() - started, 2)
    result["finished_at"] = datetime.now(UTC).isoformat()
    write(OUT / "sources" / f"{source['id']}.json", result)
    return result


def diagnosis(result):
    """Distingue ausência na janela de falhas de acesso e de conteúdo."""
    confirmed = [e for e in result["evidence"] if e["status"] == "captured"]
    if confirmed:
        message = "Notícia textual e evidência confirmadas na amostra."
    elif any(t.get("xml_valid") for t in result["tests"]):
        samples = [sample for test in result["tests"] for sample in test.get("samples", [])]
        statuses = sorted(
            {
                sample.get("status")
                for sample in samples
                if sample.get("status") in (401, 403, 429, 503)
            }
        )
        if statuses:
            message = (
                "XML funcional; acesso às matérias falhou com HTTP "
                + ", ".join(map(str, statuses))
                + "."
            )
        elif samples:
            reasons = sorted(
                {str(sample.get("reason")) for sample in samples if sample.get("reason")}
            )
            message = "XML funcional, sem notícia textual comprovada nesta amostra: " + "; ".join(
                reasons
            )
        else:
            message = "XML funcional; nenhuma matéria elegível pôde ser amostrada nesta janela/escopo. Não é falha de acesso ao XML."
    else:
        message = "Nenhum XML útil confirmado nesta amostra. " + "; ".join(
            dict.fromkeys(
                str(t.get("endpoint_error") or t.get("error") or t.get("outcome"))
                for t in result["tests"]
            )
        )
    notes = {
        25: " Atenção: cadastro 'Record' aponta para record.com.br, uma editora de livros; revisar identidade da fonte de televisão.",
        373: " A comprovação obtida é da edição chinesa do NYT (cn.nytimes.com); não comprova a edição inglesa.",
        19: " O RSS retornou URLs fora do prefixo configurado para a fonte; não foram atribuídas à GloboNews.",
        26: " Há URLs no XML, mas as amostras não comprovam a cobertura específica de Record News.",
        377: " Conteúdo Reuters Plus/publicitário foi excluído como prova de cobertura editorial.",
        1272: " A data de publicação das amostras da NHK não foi confirmada; não são contadas como notícias de hoje.",
    }
    return message + notes.get(result["source_id"], "")


def report(results, manifest):
    """Gera contagens e links de exemplos sem escrever no corpus."""
    rows = []
    for r in sorted(results, key=lambda x: x["source_id"]):
        confirmed = [e for e in r["evidence"] if e["status"] == "captured"]
        rows.append(
            dict(
                source_id=r["source_id"],
                fonte=r["name"],
                links_testados=len(r["tests"]),
                xml_acessivel=any(t.get("xml_valid") for t in r["tests"]),
                noticias_listadas=sum(t.get("news_urls", 0) for t in r["tests"]),
                textos=sum(
                    bool(s.get("text") and s.get("article") and editorial(s["url"]))
                    for t in r["tests"]
                    for s in t.get("samples", [])
                ),
                evidencias=len(confirmed),
                evidencias_hoje=sum(e["publication"] == "hoje" for e in confirmed),
                evidencias_data_anterior=sum(e["publication"] == "anterior" for e in confirmed),
                evidencias_sem_data=sum(e["publication"] == "desconhecida" for e in confirmed),
                segundos=r["duration_seconds"],
                diagnostico=diagnosis(r),
            )
        )
    summary = dict(
        started_at=manifest["started_at"],
        finished_at=datetime.now(UTC).isoformat(),
        sources=len(rows),
        sources_xml=sum(r["xml_acessivel"] for r in rows),
        sources_text=sum(r["textos"] > 0 for r in rows),
        sources_evidence=sum(r["evidencias"] > 0 for r in rows),
        sources_evidence_today=sum(r["evidencias_hoje"] > 0 for r in rows),
        evidence_total=sum(r["evidencias"] for r in rows),
        rows=rows,
    )
    write(OUT / "summary.json", summary)
    with (OUT / "fontes.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    def esc(value):
        return html.escape(str(value), quote=True)

    table = []
    for row in rows:
        result = next(r for r in results if r["source_id"] == row["source_id"])
        examples = "<br>".join(
            f'<a target="_blank" rel="noopener noreferrer" href="{esc(e["url"])}">Notícia: {esc(e["title"])}</a> · <a target="_blank" href="/evidencias/teste-prioritarios/{e["url_hash"]}">Ver evidência</a> ({esc(e["publication"])}: {esc(e["published_at"])})'
            for e in result["evidence"]
            if e["status"] == "captured"
        )
        reasons = diagnosis(result)
        routes = (
            "<details><summary>Ver links testados</summary><ul>"
            + "".join(
                f'<li><a target="_blank" rel="noopener noreferrer" href="{esc(t["url"])}">{esc(t["url"])}</a> · HTTP {esc(t.get("endpoint_status"))} · {esc(t.get("endpoint_error") or t.get("error") or t.get("outcome"))}</li>'
                for t in result["tests"]
            )
            + "</ul></details>"
        )
        table.append(
            f"<tr><td>{esc(row['fonte'])}</td><td>{row['links_testados']}</td><td>{row['noticias_listadas']}</td><td>{row['textos']}</td><td>{row['evidencias']}</td><td>{examples}<p>{esc(reasons)}</p>{routes}</td></tr>"
        )
    page = '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Teste de notícias e evidências</title><style>body{font:16px system-ui;margin:24px;color:#183b35}table{border-collapse:collapse;width:100%}td,th{padding:12px;border:1px solid #ddd;text-align:left}a{color:#006c65}</style><h1>Notícias e evidências — veículos prioritários</h1>'
    page += f"<p>{len(rows)} fontes · {summary['sources_evidence']} com evidência · {summary['sources_evidence_today']} com evidência de hoje. Finalizado: {esc(summary['finished_at'])}</p>"
    page += (
        "<p>Teste por amostragem: até 3 links e 3 matérias por link. Não mede cobertura integral. Sem novas rodadas, análise de contexto, gravação no corpus ou serviços pagos. Data anterior ou ausente é indicada separadamente. Acessar o XML sem notícias do dia não é erro de acesso.</p><table><thead><tr><th>Fonte</th><th>Links testados</th><th>URLs listadas</th><th>Textos</th><th>Evidências</th><th>Exemplos ou resultado</th></tr></thead><tbody>"
        + "".join(table)
        + "</tbody></table></html>"
    )
    (OUT / "report.html").write_text(page)
    return summary


def main():
    """Executa um lote retomável, com checkpoints separados por fonte."""
    global USED
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else freeze()
    write(manifest_path, manifest)
    USED = sum(p.stat().st_size for p in (OUT / "evidence").rglob("*.html"))
    results = [json.loads(p.read_text()) for p in (OUT / "sources").glob("*.json")]
    done = {r["source_id"] for r in results}

    # Publishers sharing a domain are serialized; other publishers run independently.
    def locked(source):
        from urllib.parse import urlsplit

        host = (urlsplit(source["url"]).hostname or "").removeprefix("www.")
        with LOCK:
            lock = HOST_LOCKS.setdefault(host, threading.Lock())
        with lock:
            return test_source(source, manifest)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(locked, s) for s in manifest["sources"] if s["id"] not in done]
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            report(results, manifest)
            print(
                json.dumps(
                    dict(
                        done=len(results),
                        total=len(manifest["sources"]),
                        source=r["name"],
                        evidence=sum(e["status"] == "captured" for e in r["evidence"]),
                        seconds=r["duration_seconds"],
                    ),
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(json.dumps(report(results, manifest), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
