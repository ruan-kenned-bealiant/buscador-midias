"""Etapa 2 da rodada 12: amostra finita, checkpoint por página e HTTP direto sem IA."""

from __future__ import annotations

import argparse
import csv
import fcntl
import gzip
import hashlib
import json
import os
import subprocess
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

import consolidate_round12 as reports
from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import canonicalize_url
from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page
from buscador_midias.net.fetcher import USER_AGENT, FetchResult, ThrottledFetcher, host_key
from buscador_midias.sites.loader import load_sites
from buscador_midias.strategy.manual import is_excluded, load_manual_rules
from buscador_midias.strategy.mechanism_inventory import safe_mechanism_url

ROOT = reports.ROOT
ROUND = reports.ROUND
INPUT = reports.OUTPUT
OUTPUT = ROOT / 'var/analise-rodada-12/etapa-2'
WORKERS = 24


def save(path: Path, data: Any) -> None:
    """Publica um checkpoint atomicamente."""
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def rows(path: Path) -> list[dict[str, str]]:
    """Lê um CSV local."""
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def identity(site: str, url: str) -> str:
    """Identidade por fonte e URL canônica, mantendo escopos distintos separados."""
    return hashlib.sha256((site + '\n' + canonicalize_url(url)).encode()).hexdigest()


def prepare() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Amostra primeira, intermediária e última entrada na ordem armazenada por mecanismo."""
    import sqlite3

    mechanisms: dict[str, dict[str, Any]] = {}
    for row in rows(INPUT / 'mecanismos.csv'):
        if row['tipo'] != 'bootstrap':
            mechanisms[identity(row['fonte'], row['url'])] = dict(
                id=identity(row['fonte'], row['url']), fonte=row['fonte'], url=row['url'],
                tipo=row['tipo'], origem='consolidacao_etapa_1', categoria_etapa1=row['categoria'],
                amostras=[],
            )
    associations: list[dict[str, Any]] = []

    def attach(mechanism_id: str, url: str, title: str, date: str, basis: str) -> None:
        mechanism = mechanisms[mechanism_id]
        sample_id = identity(mechanism['fonte'], url)
        if sample_id not in mechanism['amostras'] and len(mechanism['amostras']) < 3:
            mechanism['amostras'].append(sample_id)
            associations.append(dict(
                mecanismo_id=mechanism_id, amostra_id=sample_id, fonte=mechanism['fonte'],
                mecanismo=mechanism['url'], url=url, titulo_xml=title,
                data_xml=date, base_data_xml=basis,
            ))

    with sqlite3.connect(f'file:{ROUND / "fila-documentos.sqlite"}?mode=ro', uri=True) as queue:
        queue.row_factory = sqlite3.Row
        for row in queue.execute('''WITH ranked AS (
            SELECT root_id,url,title,date,basis,
                row_number() OVER(PARTITION BY root_id ORDER BY hash) AS position,
                count(*) OVER(PARTITION BY root_id) AS total FROM evidence)
            SELECT r.site,r.url AS mechanism,e.* FROM ranked e JOIN roots r ON r.id=e.root_id
            WHERE position=1 OR position=(total+1)/2 OR position=total
            ORDER BY r.id,position'''):
            key = identity(row['site'], row['mechanism'])
            if key in mechanisms:
                attach(key, row['url'], row['title'], row['date'], row['basis'])

    origin_totals = {(r['fonte'], canonicalize_url(r['url_origem'])): int(r['candidatas'])
                     for r in rows(INPUT / 'origens-diretas.csv')}
    positions: Counter[tuple[str, str]] = Counter()
    # Não abre índices nem descobre caminhos: somente links já exportados na rodada 12.
    with (ROUND / 'noticias-48h.csv').open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            origin = row['site_key'], canonicalize_url(row['source_sitemap'])
            position = positions[origin]
            positions[origin] += 1
            total = origin_totals[origin]
            if position not in {0, (total-1)//2, total-1}:
                continue
            key = identity(*origin)
            if key not in mechanisms:
                mechanisms[key] = dict(
                    id=key, fonte=row['site_key'], url=row['source_sitemap'], tipo='origem_direta',
                    origem='xml_filho_ou_origem_direta', categoria_etapa1='com_candidatas',
                    amostras=[],
                )
            attach(key, row['url'], row['title'], row['publication_date'] or row['lastmod'],
                   row['base_da_data'])
    save(OUTPUT / 'mecanismos-planejados.json', list(mechanisms.values()))
    save(OUTPUT / 'amostras-planejadas.json', associations)
    save(OUTPUT / 'configuracao.json', dict(
        rodada=12, etapa=2, workers=WORKERS, amostras_por_mecanismo=3,
        ordem_amostragem='primeira/intermediária/última na ordem armazenada; '
        'não é amostra aleatória',
        http_timeout_seconds=8, per_host_interval_seconds=1, max_redirects=4,
        max_body_bytes=8*1024*1024, bright_data=False, openrouter=False,
        expande_sitemaps=False, inicio=datetime.now(UTC).isoformat(),
    ))
    return list(mechanisms.values()), associations


class DirectFetcher:
    """HTTP com tamanho e tempo limitados e redirects explícitos para conferir o escopo."""

    def __init__(self, client: httpx.Client) -> None:
        """Usa cliente compartilhado sem redirects automáticos."""
        self.client = client

    def get(self, url: str) -> FetchResult:
        """Lê uma resposta, mantendo a localização de um redirect sem segui-lo."""
        started = time.monotonic()
        try:
            with self.client.stream('GET', url, headers={'User-Agent': USER_AGENT}) as response:
                kind = response.headers.get('content-type')
                if response.is_redirect:
                    target = urljoin(url, response.headers.get('location', ''))
                    return FetchResult(url, response.status_code, b'', None, target, kind)
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8*1024*1024 or time.monotonic()-started > 15:
                        return FetchResult(url, response.status_code, b'', 'body_or_time_limit')
                    chunks.append(chunk)
                return FetchResult(url, response.status_code, b''.join(chunks), None, url, kind)
        except (httpx.HTTPError, UnicodeEncodeError, ValueError) as error:
            return FetchResult(url, None, b'', type(error).__name__ + ': ' + str(error))


class ScopedReader:
    """Verifica robots e exclusões em cada salto; guarda respostas para retomada local."""

    def __init__(self, client: httpx.Client) -> None:
        """Configura acesso direto e nenhum fallback pago."""
        self.fetcher = ThrottledFetcher(DirectFetcher(client), min_interval=1)
        self.rules = load_manual_rules(ROOT / 'var/validacao-final/regras-congeladas.csv')
        self.policies: dict[str, RobotsPolicy | FetchResult] = {}
        self.lock = threading.Lock()
        self.pending: dict[str, Future[FetchResult]] = {}
        self.requests = 0
        self.cache_hits = 0
        (OUTPUT / 'http-cache').mkdir(exist_ok=True)

    def raw(self, url: str) -> FetchResult:
        """Reutiliza resposta persistente desta auditoria e reúne acessos simultâneos iguais."""
        key = hashlib.sha256(url.encode()).hexdigest()
        path = OUTPUT / 'http-cache' / (key + '.json')
        body_path = path.with_suffix('.gz')
        with self.lock:
            if path.exists():
                self.cache_hits += 1
                meta = reports.read_json(path)
                body = gzip.decompress(body_path.read_bytes()) if meta.pop('has_body') else b''
                return FetchResult(content=body, **meta)
            existing = self.pending.get(url)
            if existing is None:
                existing = Future()
                self.pending[url] = existing
                creator = True
            else:
                creator = False
        if not creator:
            return existing.result()
        try:
            result = self.fetcher.get(url)
            if result.content:
                temporary = body_path.with_suffix('.tmp')
                temporary.write_bytes(gzip.compress(result.content))
                temporary.replace(body_path)
            save(path, dict(url=result.url, status=result.status, error=result.error,
                            final_url=result.final_url, content_type=result.content_type,
                            has_body=bool(result.content)))
            with self.lock:
                self.requests += 1
            existing.set_result(result)
            return result
        except BaseException as error:
            existing.set_exception(error)
            raise
        finally:
            with self.lock:
                self.pending.pop(url, None)

    def allowed(self, site: Site, url: str) -> bool:
        """Mantém hosts próprios e exclusões manuais congeladas."""
        return safe_mechanism_url(site, url) and not is_excluded(
            url, self.rules.for_site(site).exclude)

    def policy(self, site: Site, origin: str) -> RobotsPolicy | FetchResult:
        """Lê robots uma vez; falha não é autorização para baixar artigos."""
        if origin in self.policies:
            return self.policies[origin]
        current = origin + '/robots.txt'
        result = FetchResult(current, None, b'', 'robots_redirect_limit')
        for _ in range(5):
            if not self.allowed(site, current):
                result = FetchResult(current, None, b'', 'robots_outside_scope')
                break
            result = self.raw(current)
            if result.status in (301, 302, 303, 307, 308) and result.final_url:
                current = result.final_url
                continue
            break
        policy: RobotsPolicy | FetchResult
        if result.ok:
            policy = RobotsPolicy.parse(result.content.decode('utf-8', errors='replace'),
                                        base_url=origin)
        elif result.status in (404, 410):
            policy = RobotsPolicy.allow_all()
        else:
            policy = FetchResult(current, result.status, b'', 'robots_unavailable')
        self.policies[origin] = policy
        return policy

    def fetch(self, site: Site, url: str) -> FetchResult:
        """Segue até quatro redirects conferindo o novo destino antes de requisitá-lo."""
        current = url
        for _ in range(5):
            if not self.allowed(site, current):
                return FetchResult(url, None, b'', 'outside_scope', current)
            parts = urlsplit(current)
            policy = self.policy(site, f'{parts.scheme}://{parts.netloc}')
            if isinstance(policy, FetchResult):
                return FetchResult(url, policy.status, b'', policy.error, current)
            if not policy.allows(current, USER_AGENT):
                return FetchResult(url, None, b'', 'robots_disallowed', current)
            result = self.raw(current)
            if result.status in (301, 302, 303, 307, 308) and result.final_url:
                current = result.final_url
                continue
            return FetchResult(url, result.status, result.content, result.error,
                               current, result.content_type)
        return FetchResult(url, None, b'', 'redirect_limit', current)


def audit(reader: ScopedReader, site: Site, sample: dict[str, Any],
          since: datetime, until: datetime) -> dict[str, Any]:
    """Guarda o desfecho de uma URL de amostra, com metadados da evidência."""
    result = reader.fetch(site, sample['url'])
    observed = datetime.now(UTC)
    base = dict(amostra_id=sample['amostra_id'], fonte=site.key, url=sample['url'],
                final_url=result.final_url or sample['url'], http_status=result.status,
                observado_em=observed.isoformat(), erro=result.error or '')
    if result.error or not result.ok:
        base.update(categoria='bloqueio' if result.status in (401, 403, 429) else
                    'robots_proibido' if result.error == 'robots_disallowed' else 'falha_acesso',
                    texto_util_confirmado=False, artigo_acessivel=False,
                    caracteres_texto=0, titulo='', trecho='', publicado_em='',
                    publicacao_na_janela='sem_data', tipos_schema='', sinal_audio=False,
                    audio_dedicado=False, classificacao_pagina='', motivos=result.error or 'http')
    else:
        page = PageSnapshot(result.url, result.status, result.content_type,
                            result.final_url, result.content)
        base.update(audit_page(page, since=since, until=until, now=observed))
    save(OUTPUT / 'resultados' / (sample['amostra_id'] + '.json'), base)
    return base


def consolidate(mechanisms: list[dict[str, Any]], associations: list[dict[str, Any]]) -> None:
    """Produz evidência por fonte e mecanismo; não mantém/exclui mecanismos automaticamente."""
    reports.OUTPUT = OUTPUT
    results = {p.stem: reports.read_json(p) for p in (OUTPUT / 'resultados').glob('*.json')}
    mechanism_rows = []
    source_results: dict[str, set[str]] = defaultdict(set)
    for mechanism in mechanisms:
        tested = [results[key] for key in mechanism['amostras'] if key in results]
        text = sum(r['texto_util_confirmado'] for r in tested)
        audio = sum(r['audio_dedicado'] for r in tested)
        pending = len(mechanism['amostras']) - len(tested)
        verdict = ('amostra_pendente' if pending else 'texto_e_audio_na_amostra' if text and audio
                   else 'texto_confirmado_na_amostra' if text else 'audio_na_amostra' if audio
                   else 'sem_texto_confirmado_na_amostra' if tested else 'sem_amostra_na_janela')
        mechanism_rows.append(dict(
            fonte=mechanism['fonte'], mecanismo=mechanism['url'], tipo=mechanism['tipo'],
            origem=mechanism['origem'], categoria_etapa1=mechanism['categoria_etapa1'],
            resultado_amostra=verdict, amostras_planejadas=len(mechanism['amostras']),
            amostras_verificadas=len(tested), com_texto=text, audio_podcast=audio,
            publicacao_na_janela=sum(r['publicacao_na_janela'] == 'sim' for r in tested),
            publicacao_fora_da_janela=sum(r['publicacao_na_janela'] == 'nao' for r in tested),
            sem_data_publicacao=sum(r['publicacao_na_janela'] == 'sem_data' for r in tested),
            exemplo_textual=next((r['url'] for r in tested if r['texto_util_confirmado']), ''),
            exemplo_audio=next((r['url'] for r in tested if r['audio_dedicado']), ''),
        ))
        source_results[mechanism['fonte']].update(mechanism['amostras'])
    source_rows = []
    for source in rows(INPUT / 'fontes.csv'):
        tested = [results[key] for key in source_results[source['fonte']] if key in results]
        source_rows.append(dict(
            fonte=source['fonte'], nome=source['nome'], categoria_etapa1=source['categoria'],
            amostras_verificadas=len(tested),
            amostras_com_texto=sum(r['texto_util_confirmado'] for r in tested),
            amostras_audio=sum(r['audio_dedicado'] for r in tested),
            publicacao_na_janela=sum(r['publicacao_na_janela'] == 'sim' for r in tested),
            texto_e_publicacao_na_janela=sum(
                r['texto_util_confirmado'] and r['publicacao_na_janela'] == 'sim' for r in tested),
            exemplo_textual=next((r['url'] for r in tested if r['texto_util_confirmado']), ''),
        ))
    evidence_rows = [dict(**ref, **{k: v for k, v in results[ref['amostra_id']].items()
                                   if k not in ref})
                     for ref in associations if ref['amostra_id'] in results]
    summary = dict(
        etapa=2, rodada=12, paginas_verificadas=len(results),
        paginas_planejadas=len({r['amostra_id'] for r in associations}),
        resultado_paginas=dict(Counter(r['categoria'] for r in results.values())),
        fontes_com_texto=sum(r['amostras_com_texto'] > 0 for r in source_rows),
        fontes_com_texto_e_publicacao_na_janela=sum(
            r['texto_e_publicacao_na_janela'] > 0 for r in source_rows),
        mecanismos_por_resultado=dict(Counter(r['resultado_amostra'] for r in mechanism_rows)),
        criterios=[
            'Até três candidatas por mecanismo, primeira/intermediária/última na ordem armazenada.',
            'Amostra determinística, não aleatória; não estima percentuais de toda a população.',
            'Um sucesso confirma a amostra, não toda a editoria nem estabilidade futura.',
            'Sem amostra na janela não reprova o mecanismo. '
            'Falha atual não prova invalidade permanente.',
            'NewsArticle com áudio embutido preserva texto; PodcastEpisode/AudioObject sem artigo '
            'estruturado é sinal de áudio dedicado. URL/título sozinho não prova áudio.',
            'Classificação automática conservadora, sujeita a revisão nas amostras exportadas.',
            'Mecanismos com áudio na amostra não são declarados exclusivamente de áudio.',
            'Nenhuma seleção/exclusão de mecanismo nesta etapa; sem Bright Data e sem OpenRouter.',
            'Consulta apenas URLs de artigos já coletadas; '
            'sem expansão de índices ou novas fontes.',
        ],
    )
    save(OUTPUT / 'resumo.json', summary)
    reports.write_csv('fontes.csv', source_rows)
    reports.write_csv('mecanismos.csv', mechanism_rows)
    reports.write_csv('amostras.csv', evidence_rows)
    reports.workbook({'Fontes': source_rows, 'Mecanismos': mechanism_rows,
                      'Amostras': evidence_rows,
                      'Critérios': [dict(criterio=s) for s in summary['criterios']]})
    (OUTPUT / 'consolidacao.xlsx').replace(OUTPUT / 'validacao-amostras.xlsx')


def run(prepare_only: bool) -> None:
    """Executa somente o plano finito de amostras, com retomada por resultado persistido."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'resultados').mkdir(exist_ok=True)
    plan_path = OUTPUT / 'amostras-planejadas.json'
    if plan_path.exists():
        mechanisms = reports.read_json(OUTPUT / 'mecanismos-planejados.json')
        associations = reports.read_json(plan_path)
    else:
        mechanisms, associations = prepare()
    samples = {r['amostra_id']: r for r in associations}
    assert all(len(m['amostras']) <= 3 for m in mechanisms)
    assert all(r['url'].startswith(('http://', 'https://')) for r in associations)
    completed = {p.stem for p in (OUTPUT / 'resultados').glob('*.json')}
    status = dict(etapa=2, rodada=12, status='prepared', pid=os.getpid(),
                  paginas_planejadas=len(samples),
                  paginas_concluidas=len(completed), mecanismos_com_amostras=sum(
                      bool(m['amostras']) for m in mechanisms), expansao_de_indices=False,
                  bright_data=False, openrouter=False)
    save(OUTPUT / 'execucao.json', status)
    print(json.dumps(status, ensure_ascii=False), flush=True)
    if prepare_only:
        return
    catalog = load_sites(ROOT / 'var/validacao-final/catalogo-congelado.csv')
    sites = {s.key: s for s in catalog.sites}
    window = reports.read_json(ROUND / 'execucao.json')
    since, until = (datetime.fromisoformat(window[k]) for k in ('window_since', 'window_until'))
    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for key, sample in samples.items():
        if key not in completed:
            queues[host_key(sample['url'])].append(sample)
    hosts = deque(queues)
    active: dict[Future[dict[str, Any]], str] = {}
    outcomes: Counter[str] = Counter()
    last_publish = 0.0
    with httpx.Client(timeout=8, follow_redirects=False,
                      limits=httpx.Limits(max_connections=WORKERS*2)) as client:
        reader = ScopedReader(client)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            while hosts or active:
                busy = set(active.values())
                for _ in range(len(hosts)):
                    host = hosts.popleft()
                    if queues[host] and host not in busy and len(active) < WORKERS:
                        sample = queues[host].popleft()
                        future = pool.submit(audit, reader, sites[sample['fonte']], sample,
                                             since, until)
                        active[future] = host
                        busy.add(host)
                    if queues[host]:
                        hosts.append(host)
                if active:
                    ready, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                    for future in ready:
                        result = future.result()
                        completed.add(result['amostra_id'])
                        outcomes[result['categoria']] += 1
                        del active[future]
                if time.monotonic()-last_publish > 5:
                    last_publish = time.monotonic()
                    status.update(status='running', paginas_concluidas=len(completed),
                                  updated_at=datetime.now(UTC).isoformat(),
                                  resultados_nesta_execucao=dict(outcomes),
                                  chamadas_http_nesta_execucao=reader.requests)
                    save(OUTPUT / 'execucao.json', status)
        consolidate(mechanisms, associations)
    status.update(status='completed', paginas_concluidas=len(completed),
                  updated_at=datetime.now(UTC).isoformat())
    save(OUTPUT / 'execucao.json', status)
    print(json.dumps(status, ensure_ascii=False), flush=True)
    subprocess.run(['notify-send', 'Buscador de mídias — etapa 2 concluída',
                    f'{len(completed)} páginas verificadas. Relatórios em {OUTPUT}'], check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / '.execucao.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            run(args.prepare_only)
        except BaseException as error:
            status_path = OUTPUT / 'execucao.json'
            status = reports.read_json(status_path) if status_path.exists() else {}
            status.update(status='interrupted', error=type(error).__name__ + ': ' + str(error),
                          updated_at=datetime.now(UTC).isoformat())
            save(status_path, status)
            raise
