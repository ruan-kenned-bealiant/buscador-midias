"""Rodada finita de resolução: somente inconclusivos congelados, sem descoberta recursiva."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

import consolidate_round12 as reports
import export_round12_selection as exporter
import validate_round12_samples as samples
from buscador_midias.analysis.mechanism_selection import MechanismEvidence, classify
from buscador_midias.discovery.xml_review import review_xml, transient
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page
from buscador_midias.net.fetcher import FetchResult, host_key
from buscador_midias.sites.loader import load_sites

ROOT = reports.ROOT
BASE = ROOT / "var/analise-rodada-12"
OUT = BASE / "resolucao-inconclusivos"
WORKERS = 24


def identity(site: str, url: str) -> str:
    """Mantém identidades consistentes com as etapas anteriores."""
    return samples.identity(site, url)


def prepare() -> dict[str, Any]:
    """Congela entradas, prioridade das 35 fontes e evidência histórica local."""
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("resultados", "http-cache", "revisao"):
        (OUT / name).mkdir(exist_ok=True)
    if (OUT / "plano.json").exists():
        saved: dict[str, Any] = reports.read_json(OUT / "plano.json")
        return saved
    shutil.copytree(BASE / "etapa-5", OUT / "entrega-anterior", dirs_exist_ok=True)
    sources = samples.rows(BASE / "etapa-4/fontes.csv")
    gaps = {r["fonte"] for r in sources if r["lacuna_proveniencia"] == "True"}
    historical = {
        (r["site_key"], canonicalize_url(r["root"])): r
        for r in samples.rows(ROOT / "var/validacao-final/acesso.csv")
    }
    jobs: list[dict[str, Any]] = []
    for row in samples.rows(BASE / "etapa-4/mecanismos.csv"):
        if row["classificacao"] != "inconclusivo":
            continue
        previous = historical.get((row["fonte"], canonicalize_url(row["mecanismo"])), {})
        jobs.append(
            dict(
                id=identity(row["fonte"], row["mecanismo"]),
                fonte=row["fonte"],
                url=row["mecanismo"],
                prioritario=row["fonte"] in gaps,
                motivo_anterior=row["motivo"],
                xml_historico=previous.get("xml_readable") == "True",
                texto_historico=previous.get("readable") == "True",
                exemplo_historico=previous.get("example_url", ""),
            )
        )
    jobs.sort(
        key=lambda r: (
            not r["prioritario"],
            not r["texto_historico"],
            not r["xml_historico"],
            r["fonte"],
            r["url"],
        )
    )
    assert len(jobs) == 8591 and len(gaps) == 35
    plan = dict(
        jobs=jobs,
        fontes_prioritarias=sorted(gaps),
        workers=WORKERS,
        endpoints_http=sum(j["url"].startswith(("http://", "https://")) for j in jobs),
        amostras_por_endpoint=3,
        tentativas=1,
        repeticoes_transitorias=1,
        expansao_recursiva=False,
        novos_endpoints=False,
        bright_data=False,
        openrouter=False,
        gerado_em=datetime.now(UTC).isoformat(),
    )
    samples.save(OUT / "plano.json", plan)
    return plan


def fetch_once_with_retry(
    reader: samples.ScopedReader, site: Any, url: str
) -> tuple[FetchResult, int]:
    """Repete uma vez após remover só o cache transitório pertinente a este host."""
    result = reader.fetch(site, url)
    if not transient(result):
        return result, 1
    parts = urlsplit(result.final_url or url)
    origin = f"{parts.scheme}://{parts.netloc}"
    policy = reader.policies.get(origin)
    retry_subject = policy.url if isinstance(policy, FetchResult) else (result.final_url or url)
    retry_directory = OUT / "repeticoes"
    retry_directory.mkdir(exist_ok=True)
    marker = retry_directory / (hashlib.sha256(retry_subject.encode()).hexdigest() + ".json")
    try:
        with marker.open("x") as stream:
            json.dump({"url": retry_subject, "claimed_at": datetime.now(UTC).isoformat()}, stream)
    except FileExistsError:
        # A mesma falha/robots encontrado por outro mecanismo não renova a tentativa.
        return result, 2
    retry_urls = {url, result.final_url or url, origin + "/robots.txt"}
    if isinstance(policy, FetchResult):
        retry_urls.add(policy.url)
    for candidate in retry_urls:
        path = OUT / "http-cache" / (hashlib.sha256(candidate.encode()).hexdigest() + ".json")
        with reader.lock:
            if path.exists():
                meta = reports.read_json(path)
                if transient(FetchResult(candidate, meta["status"], b"", meta["error"])):
                    path.unlink()
    if isinstance(policy, FetchResult) and transient(policy):
        reader.policies.pop(origin, None)
    result = reader.fetch(site, url)
    return result, 2


def inspect(
    job: dict[str, Any],
    reader: samples.ScopedReader,
    sites: dict[str, Any],
    previous: dict[tuple[str, str], dict[str, Any]],
    known: dict[tuple[str, str], str],
    until: datetime,
) -> dict[str, Any]:
    """Verifica um documento e até três artigos; índices não geram novas requisições."""
    url, site = job["url"], sites[job["fonte"]]
    record = dict(
        **job,
        observado_em=datetime.now(UTC).isoformat(),
        xml_valido=False,
        tipo="",
        estado="pendente",
        detalhe="",
        http_status=None,
        tentativas=0,
        candidatas=0,
        filhos_listados=0,
        filhos_conhecidos=[],
        amostras=[],
        texto_confirmado=False,
        exemplo_textual="",
        podcast_xml=False,
        caminho_evidencia=[],
    )
    if not url.startswith(("http://", "https://")):
        record.update(estado="metadado_nao_endpoint", detalhe="Rótulo de procedência, não URL HTTP")
    else:
        result, attempts = fetch_once_with_retry(reader, site, url)
        record.update(http_status=result.status, tentativas=attempts, final_url=result.final_url)
        if not result.ok:
            record.update(
                estado="impedimento_acesso", detalhe=result.error or f"HTTP {result.status}"
            )
        else:
            try:
                xml = review_xml(result.content, result.final_url or url)
            except (ValueError, OverflowError) as error:
                record.update(estado="xml_nao_confirmado", detalhe=str(error)[:500])
            else:
                record.update(
                    xml_valido=True,
                    tipo=xml.kind,
                    estado="xml_confirmado",
                    candidatas=len(xml.entries),
                    filhos_listados=len(xml.children),
                    podcast_xml=xml.podcast,
                )
                record["filhos_conhecidos"] = list(
                    dict.fromkeys(
                        known[(site.key, canonicalize_url(child))]
                        for child in xml.children
                        if (site.key, canonicalize_url(child)) in known
                    )
                )
                candidates = [u for u in xml.entries if reader.allowed(site, u)]
                # Preferir prova textual já disponível que ainda aparece no XML atual.
                matches = [
                    u
                    for u in candidates
                    if previous.get((site.key, canonicalize_url(u)), {}).get(
                        "texto_util_confirmado"
                    )
                ]
                selected = (
                    list(
                        dict.fromkeys(
                            matches[:1]
                            + [
                                candidates[i]
                                for i in sorted(
                                    {0, (len(candidates) - 1) // 2, len(candidates) - 1}
                                )
                            ]
                        )
                    )[:3]
                    if candidates
                    else []
                )
                for article in selected:
                    old = previous.get((site.key, canonicalize_url(article)))
                    if old and old["texto_util_confirmado"]:
                        observation = dict(old, reutilizada_etapa2=True)
                    else:
                        fetched, retries = fetch_once_with_retry(reader, site, article)
                        observation = dict(
                            url=article,
                            http_status=fetched.status,
                            texto_util_confirmado=False,
                            audio_dedicado=False,
                            reutilizada_etapa2=False,
                            tentativas=retries,
                            categoria="impedimento_acesso",
                            erro=fetched.error or "",
                        )
                        if fetched.ok:
                            observation.update(
                                audit_page(
                                    PageSnapshot(
                                        article,
                                        fetched.status,
                                        fetched.content_type,
                                        fetched.final_url,
                                        fetched.content,
                                    ),
                                    since=until - timedelta(days=2),
                                    until=until,
                                    now=datetime.now(UTC),
                                )
                            )
                    record["amostras"].append(observation)
                readable = [r for r in record["amostras"] if r["texto_util_confirmado"]]
                if readable:
                    record.update(
                        texto_confirmado=True,
                        exemplo_textual=readable[0]["url"],
                        estado="texto_confirmado",
                        caminho_evidencia=[url],
                    )
    samples.save(OUT / "resultados" / (job["id"] + ".json"), record)
    return record


def publish(plan: dict[str, Any]) -> None:
    """Produz revisão separada e atualiza o painel, preservando as etapas antigas."""
    records = {
        j["id"]: reports.read_json(OUT / "resultados" / (j["id"] + ".json")) for j in plan["jobs"]
    }
    # Propagar apenas ligações explicitamente declaradas entre documentos já verificados.
    for _ in range(10):
        changed = False
        for record in records.values():
            if not record["xml_valido"] or record["texto_confirmado"]:
                continue
            for child in record["filhos_conhecidos"]:
                proof = records[child]
                chain = proof["caminho_evidencia"]
                if proof["texto_confirmado"] and len(chain) < 10 and record["url"] not in chain:
                    record.update(
                        texto_confirmado=True,
                        exemplo_textual=proof["exemplo_textual"],
                        estado="texto_confirmado_por_filho_conhecido",
                        caminho_evidencia=[record["url"], *chain],
                    )
                    changed = True
                    break
        if not changed:
            break
    mechanisms: list[dict[str, Any]] = samples.rows(BASE / "etapa-4/mecanismos.csv")
    transitions: Counter[str] = Counter()
    for row in mechanisms:
        record = records.get(identity(row["fonte"], row["mecanismo"]))
        row.update(
            revisao_estado="",
            revisao_observado_em="",
            revisao_detalhe="",
            revisao_caminho_evidencia="",
        )
        if record is None:
            continue
        row.update(
            revisao_estado=record["estado"],
            revisao_observado_em=record["observado_em"],
            revisao_detalhe=record["detalhe"],
            revisao_caminho_evidencia=" | ".join(record["caminho_evidencia"]),
        )
        if record["estado"] == "metadado_nao_endpoint":
            row.update(
                classificacao="excluir",
                motivo="metadado_de_procedencia_nao_e_endpoint",
                explicacao="Rótulo de procedência; não é RSS/sitemap. "
                "Artigos e evidências preservados.",
            )
        elif record["xml_valido"]:
            useful = max(int(row["textos_confirmados"]), int(record["texto_confirmado"]))
            audio = sum(bool(a["audio_dedicado"]) for a in record["amostras"])
            decision, reason = classify(
                MechanismEvidence(
                    usable_http=True,
                    xml_confirmed=True,
                    useful_texts=useful,
                    sampled=len(record["amostras"]),
                    sampled_audio=audio,
                    dedicated_audio_proven=record["podcast_xml"],
                    documents=int(row["documentos_processados"])
                    if row["documentos_processados"]
                    else None,
                    pending=int(row["documentos_pendentes"]),
                )
            )
            row.update(
                classificacao=decision,
                motivo="revisao_finita_" + reason,
                explicacao=(
                    "XML verificado e notícia textual comprovada, com vínculo preservado."
                    if useful
                    else "XML verificado, sem texto confirmado nesta amostra; "
                    "preservar o resultado e o impedimento identificado."
                ),
                tipo_reutilizavel=record["tipo"],
                endpoint_reutilizavel="True",
                http_no_escopo="True",
                xml_confirmado="True",
                textos_confirmados=str(useful),
                exemplo_textual=record["exemplo_textual"] or row["exemplo_textual"],
            )
        else:
            row.update(
                motivo="impedimento_identificado_" + record["estado"],
                explicacao="Verificação encerrada sem XML funcional comprovado: "
                + record["detalhe"],
            )
        row["uso_proposto"] = {
            "frequente": "Busca frequente",
            "periodico": "Verificação periódica",
            "excluir": "Excluir da seleção textual",
            "inconclusivo": "Inconclusivo",
        }[row["classificacao"]]
        transitions[row["classificacao"]] += 1
    source_rows: list[dict[str, Any]] = samples.rows(BASE / "etapa-4/fontes.csv")
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mechanisms:
        by_source[row["fonte"]].append(row)
    for source in source_rows:
        members = by_source[source["fonte"]]
        counts = Counter(r["classificacao"] for r in members)
        has_text = any(int(r["textos_confirmados"]) for r in members)
        source.update(
            frequentes=counts["frequente"],
            periodicos=counts["periodico"],
            excluidos=counts["excluir"],
            inconclusivos=counts["inconclusivo"],
            possui_texto_confirmado=str(has_text),
            possui_endpoint_frequente=str(bool(counts["frequente"])),
            lacuna_proveniencia=str(has_text and not counts["frequente"]),
            exemplo_endpoint_frequente=next(
                (r["mecanismo"] for r in members if r["classificacao"] == "frequente"), ""
            ),
        )
    assert (
        sum(transitions.values()) == 8591 and len(mechanisms) == 40502 and len(source_rows) == 1294
    )
    revision = OUT / "revisao"
    reports.OUTPUT = revision
    reports.write_csv("mecanismos.csv", mechanisms)
    reports.write_csv("fontes.csv", source_rows)
    gaps = [
        r
        for r in mechanisms
        if int(r["textos_confirmados"]) and r["endpoint_reutilizavel"] != "True"
    ]
    if not gaps:
        gaps = [dict(fonte="", mecanismo="", explicacao="Nenhuma lacuna deste tipo nesta revisão")]
    reports.write_csv("lacunas-proveniencia.csv", gaps)
    reviews = [
        dict(
            fonte=r["fonte"],
            url=r["url"],
            estado=r["estado"],
            tipo=r["tipo"],
            xml_valido=r["xml_valido"],
            texto_confirmado=r["texto_confirmado"],
            http_status=r["http_status"],
            tentativas=r["tentativas"],
            detalhe=r["detalhe"],
            exemplo=r["exemplo_textual"],
        )
        for r in records.values()
    ]
    reports.write_csv("verificacoes.csv", reviews)
    criteria = [
        dict(criterio=s)
        for s in [
            "Lista congelada: 8545 HTTP e 46 rótulos, sem novos endpoints ou expansão recursiva.",
            "Uma tentativa direta e uma repetição somente para falha transitória identificada.",
            "Até três artigos por documento, sem filtro de publicação "
            "para validar utilidade do caminho.",
            "Amostras antigas confirmadas são reaproveitadas somente "
            "se a URL consta no XML verificado.",
            "Propagação local por até dez ligações entre índices/filhos "
            "já verificados na lista fixa.",
            "Bloqueio, 404 e falha não excluem definitivamente a fonte; "
            "ficam pendentes com motivo.",
            "Rótulos de procedência excluídos da seleção não são mecanismos; "
            "os artigos são preservados.",
            "Dados das etapas 1 a 4 e entrega anterior são preservados; o painel recebe a revisão.",
        ]
    ]
    reports.workbook(
        {
            "Fontes": source_rows,
            "Mecanismos": mechanisms,
            "Verificações": reviews,
            "Lacunas": gaps,
            "Critérios": criteria,
        }
    )
    (revision / "consolidacao.xlsx").replace(revision / "classificacao-mecanismos.xlsx")
    exporter.INPUT = revision
    exporter.main()
    summary = dict(
        inconclusivos_originais=8591,
        resolvidos=sum(v for k, v in transitions.items() if k != "inconclusivo"),
        pendentes_com_impedimento=transitions["inconclusivo"],
        destinos=dict(transitions),
        fontes_frequentes=sum(int(r["frequentes"]) > 0 for r in source_rows),
        lacunas_restantes=sum(r["lacuna_proveniencia"] == "True" for r in source_rows),
        fontes_prioritarias_resolvidas=[
            r["fonte"]
            for r in source_rows
            if r["fonte"] in plan["fontes_prioritarias"] and int(r["frequentes"]) > 0
        ],
    )
    samples.save(OUT / "resumo.json", summary)


def run(prepare_only: bool) -> None:
    """Executa uma única fila fechada e publica a revisão ao concluir."""
    plan = prepare()
    if prepare_only:
        print(
            json.dumps({k: v for k, v in plan.items() if k != "jobs"}, ensure_ascii=False),
            flush=True,
        )
        return
    samples.OUTPUT = OUT
    sites = {
        s.key: s for s in load_sites(ROOT / "var/validacao-final/catalogo-congelado.csv").sites
    }
    previous = {}
    for path in (BASE / "etapa-2/resultados").glob("*.json"):
        value = reports.read_json(path)
        previous[(value["fonte"], canonicalize_url(value["url"]))] = value
    known = {(j["fonte"], canonicalize_url(j["url"])): j["id"] for j in plan["jobs"]}
    done = {p.stem for p in (OUT / "resultados").glob("*.json")}
    priorities = {j["id"] for j in plan["jobs"] if j["prioritario"]}
    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for job in plan["jobs"]:
        if job["id"] not in done:
            queues[host_key(job["url"])].append(job)
    hosts = deque(queues)
    active: dict[Future[dict[str, Any]], str] = {}
    progress = dict(
        status="running",
        pid=os.getpid(),
        total=len(plan["jobs"]),
        concluidos=len(done),
        prioritarios_total=len(priorities),
        prioritarios_concluidos=len(done & priorities),
        expansao_recursiva=False,
        bright_data=False,
        openrouter=False,
    )
    last = 0.0
    until = datetime.now(UTC)
    with httpx.Client(
        timeout=8, follow_redirects=False, limits=httpx.Limits(max_connections=WORKERS * 2)
    ) as client:
        reader = samples.ScopedReader(client)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            while hosts or active:
                busy = set(active.values())
                for _ in range(len(hosts)):
                    host = hosts.popleft()
                    if queues[host] and host not in busy and len(active) < WORKERS:
                        job = queues[host].popleft()
                        active[pool.submit(inspect, job, reader, sites, previous, known, until)] = (
                            host
                        )
                        busy.add(host)
                    if queues[host]:
                        hosts.append(host)
                if active:
                    ready, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                    for future in ready:
                        done.add(future.result()["id"])
                        del active[future]
                if time.monotonic() - last > 5:
                    last = time.monotonic()
                    progress.update(
                        concluidos=len(done),
                        prioritarios_concluidos=len(done & priorities),
                        updated_at=datetime.now(UTC).isoformat(),
                        chamadas_http=reader.requests,
                    )
                    samples.save(OUT / "execucao.json", progress)
    progress.update(
        status="consolidating", concluidos=len(done), updated_at=datetime.now(UTC).isoformat()
    )
    samples.save(OUT / "execucao.json", progress)
    publish(plan)
    progress.update(status="completed", updated_at=datetime.now(UTC).isoformat())
    samples.save(OUT / "execucao.json", progress)
    subprocess.run(
        [
            "notify-send",
            "Buscador — revisão dos inconclusivos concluída",
            "Relatórios consolidados e seleção do painel atualizada.",
        ],
        check=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / ".execucao.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            run(args.prepare_only)
        except BaseException as error:
            samples.save(
                OUT / "execucao.json",
                dict(
                    status="interrupted",
                    error=type(error).__name__ + ": " + str(error),
                    updated_at=datetime.now(UTC).isoformat(),
                ),
            )
            raise
