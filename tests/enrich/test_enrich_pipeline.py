import threading
from datetime import UTC, datetime

import pytest

from buscador_midias.analysis.context import AnalysisItem, ContextVerdict
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.enrich.pipeline import enrich_pending
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.state.store import (
    EvidenceRecord,
    FindingUrl,
    StateStore,
    TriageRecord,
    UrlKind,
)
from buscador_midias.triage.openrouter import Usage

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TEXT = (
    "A Polícia Federal prendeu o empresário João Silva nesta quarta-feira por suspeita de fraude "
    "em licitação em contratos da prefeitura. Segundo a investigação, o esquema desviou recursos. "
)
NEUTRAL = (
    "O atacante Vinícius Júnior terminou o relacionamento com a namorada, segundo pessoas "
    "próximas ao jogador, e deve seguir focado na temporada pelo clube nas próximas semanas. "
)
QUIET = "O prefeito inaugurou a nova praça no centro da cidade com a presença de moradores e artistas locais. "


def page_html(text: str, title: str = "Titulo da matéria", paragraphs: int = 8) -> bytes:
    body = "".join(f"<p>{text}</p>" for _ in range(paragraphs))
    return (
        f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta property="article:published_time" content="2026-10-14T10:00:00Z"></head>'
        f"<body><article><h1>{title}</h1>{body}</article></body></html>"
    ).encode()


def found(path: str, *, title: str | None = None) -> DiscoveredUrl:
    return DiscoveredUrl(
        url=f"https://a.com/{path}",
        url_hash=f"h-{path}",
        site_key="a.com",
        source_sitemap="https://a.com/s.xml",
        lastmod=None,
        title=title,
        publication_date=None,
    )


def seeded(*paths: str, titles: dict[str, str] | None = None) -> StateStore:
    store = StateStore()
    run = store.start_run(NOW)
    store.record_urls(
        [found(p, title=(titles or {}).get(p)) for p in paths],
        run_id=run,
        now=NOW,
        kind=UrlKind.NEW,
    )
    triage = store.start_triage_run(NOW, model="m", prompt_version="v")
    store.record_triage(
        [TriageRecord(f"h-{p}", "keep", (), "") for p in paths], triage_run_id=triage, now=NOW
    )
    return store


class FakeArticles:
    def __init__(self, pages: dict[str, PageFetch] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def add_ok(self, path: str, content: bytes) -> None:
        url = f"https://a.com/{path}"
        self.pages[url] = PageFetch(url, PageStatus.OK, 200, content, "text/html", url)

    def add_status(self, path: str, status: PageStatus, http: int | None = None) -> None:
        url = f"https://a.com/{path}"
        self.pages[url] = PageFetch(url, status, http, error=status.value)

    def fetch(self, url: str) -> PageFetch:
        with self._lock:
            self.calls.append(url)
        return self.pages.get(url) or PageFetch(url, PageStatus.NOT_FOUND, 404, error="HTTP 404")


class FakeAnalyzer:
    """Achado quando o texto cita 'fraude'; indeciso para itens cuja manchete contém 'mudo'."""

    def __init__(self) -> None:
        self.items: list[AnalysisItem] = []
        self.batches: list[int] = []
        self._lock = threading.Lock()

    def analyze(self, items):
        with self._lock:
            self.items.extend(items)
            self.batches.append(len(items))
        out = []
        for item in items:
            if "mudo" in item.headline:
                out.append(ContextVerdict(item.id, False, False, (), (), "", "", 0.0, item.basis))
                continue
            if "Vinícius" in item.text + item.headline:  # só entidade, sem fato adverso
                out.append(
                    ContextVerdict(
                        item.id,
                        True,
                        False,
                        (),
                        ({"name": "Vinícius Júnior", "type": "PF", "role": "outro"},),
                        "",
                        "resumo",
                        0.9,
                        item.basis,
                    )
                )
                continue
            adverse = "fraude" in (item.text + " " + item.headline).lower()
            out.append(
                ContextVerdict(
                    item.id,
                    True,
                    adverse,
                    ("fiscal",) if adverse else (),
                    ({"name": "João Silva", "type": "PF", "role": "acusado"},) if adverse else (),
                    "preso por fraude" if adverse else "",
                    "resumo",
                    0.9,
                    item.basis,
                )
            )
        return out


class FakeCapturer:
    def __init__(self, provider: str = "direct") -> None:
        self.calls: list[tuple[FindingUrl, PageFetch | None, str]] = []
        self.provider = provider
        self._lock = threading.Lock()

    def capture(self, finding, direct, *, expected_headline):
        with self._lock:
            self.calls.append((finding, direct, expected_headline))
        return EvidenceRecord(
            finding.url_hash,
            "captured",
            self.provider,
            f"p/{finding.url_hash}.html",
            "ab" * 32,
            100,
            "text/html",
            200,
            finding.url,
            (),
            None,
        )


def run(store, articles, analyzer=None, capturer=None, **kwargs):
    defaults = {
        "articles": articles,
        "analyzer": analyzer or FakeAnalyzer(),
        "capturer": capturer or FakeCapturer(),
        "now": NOW,
        "model": "m",
        "prompt_version": "ctx-v1",
        "evidence_mode": "auto",
        "batch_size": 2,
        "workers": 1,
        "chunk_size": 50,
    }
    defaults.update(kwargs)
    return enrich_pending(store, **defaults)


def test_fluxo_completo_texto_analise_e_evidencia_do_achado():
    store = seeded("fraude-1", "praca-2")
    articles = FakeArticles()
    articles.add_ok("fraude-1", page_html(TEXT, "PF prende empresário"))
    articles.add_ok("praca-2", page_html(QUIET, "Prefeito inaugura praça"))
    capturer = FakeCapturer()

    summary = run(store, articles, capturer=capturer)

    content = store.get_content("h-fraude-1")
    assert content is not None and content.status == "ok"
    assert content.title == "PF prende empresário"
    assert content.language == "pt-BR"
    assert content.published_at == datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    assert "Polícia Federal" in (content.lede or "")
    assert store.get_analysis("h-fraude-1").finding is True  # type: ignore[union-attr]
    assert store.get_analysis("h-praca-2").finding is False  # type: ignore[union-attr]
    assert [c[0].url_hash for c in capturer.calls] == ["h-fraude-1"]
    assert capturer.calls[0][0].entity_names == ("João Silva",)
    assert capturer.calls[0][1] is not None and capturer.calls[0][1].status is PageStatus.OK
    assert store.get_evidence("h-fraude-1").provider == "direct"  # type: ignore[union-attr]
    totals = summary.totals
    assert (totals.urls, totals.fetched_ok, totals.analyzed, totals.findings) == (2, 2, 2, 1)
    assert (totals.evidence_reused, totals.evidence_brightdata, totals.evidence_failed) == (1, 0, 0)


def test_o_modelo_recebe_titulo_da_pagina_descricao_e_texto():
    store = seeded("fraude-1")
    articles = FakeArticles()
    articles.add_ok(
        "fraude-1",
        page_html(TEXT, "PF prende empresário").replace(
            b"</head>", b'<meta property="og:description" content="Resumo da PF"></head>'
        ),
    )
    analyzer = FakeAnalyzer()

    run(store, articles, analyzer)

    [item] = analyzer.items
    assert (item.domain, item.headline, item.description, item.basis) == (
        "a.com",
        "PF prende empresário",
        "Resumo da PF",
        "text",
    )
    assert "Polícia Federal" in item.text


@pytest.mark.parametrize(
    ("status", "http"),
    [(PageStatus.BLOCKED, 403), (PageStatus.NOT_FOUND, 404), (PageStatus.ROBOTS_DISALLOWED, None)],
)
def test_sem_texto_a_analise_usa_so_a_manchete_com_base_headline(status, http):
    store = seeded("fraude-bloqueada-1", titles={"fraude-bloqueada-1": "PF prende por fraude"})
    articles = FakeArticles()
    articles.add_status("fraude-bloqueada-1", status, http)
    analyzer = FakeAnalyzer()
    capturer = FakeCapturer(provider="brightdata")

    summary = run(store, articles, analyzer, capturer)

    [item] = analyzer.items
    assert (item.basis, item.headline, item.text) == ("headline", "PF prende por fraude", "")
    assert store.get_content("h-fraude-bloqueada-1").status == status.value  # type: ignore[union-attr]
    assert store.get_analysis("h-fraude-bloqueada-1").basis == "headline"  # type: ignore[union-attr]
    # o capturador recebe o desfecho do download direto para justificar o Bright Data
    assert capturer.calls[0][1] is not None and capturer.calls[0][1].status is status
    assert summary.totals.evidence_brightdata == 1


def test_contagem_de_downloads_por_desfecho():
    store = seeded("a-1", "b-2", "c-3", "d-4")
    articles = FakeArticles()
    articles.add_ok("a-1", page_html(QUIET))
    articles.add_status("b-2", PageStatus.BLOCKED, 403)
    articles.add_status("c-3", PageStatus.NOT_FOUND, 404)
    articles.add_status("d-4", PageStatus.ERROR, None)

    totals = run(store, articles).totals

    assert (totals.fetched_ok, totals.fetched_blocked, totals.fetched_other) == (1, 1, 2)


def test_erro_transitorio_de_rede_adia_a_analise_ate_esgotar_as_tentativas():
    store = seeded("fraude-1", titles={"fraude-1": "PF prende por fraude"})
    articles = FakeArticles()
    articles.add_status("fraude-1", PageStatus.ERROR, None)
    analyzer = FakeAnalyzer()

    for _ in range(2):
        run(store, articles, analyzer, max_fetch_attempts=3)
        assert store.get_analysis("h-fraude-1") is None  # segue pendente
    assert analyzer.items == []

    run(store, articles, analyzer, max_fetch_attempts=3)

    analysis = store.get_analysis("h-fraude-1")
    assert analysis is not None and analysis.basis == "headline"
    assert store.fetch_attempts("h-fraude-1") == 3


def test_pagina_de_erro_com_status_200_nao_serve_de_texto():
    store = seeded("fraude-1", titles={"fraude-1": "PF prende por fraude"})
    articles = FakeArticles()
    articles.add_ok(
        "fraude-1",
        page_html("Página não encontrada. 404 not found.", "Página não encontrada - 404"),
    )
    analyzer = FakeAnalyzer()

    run(store, articles, analyzer)

    assert store.get_content("h-fraude-1").status == "unusable"  # type: ignore[union-attr]
    assert analyzer.items[0].basis == "headline"


def test_pagina_sem_texto_suficiente_nao_serve_de_texto():
    store = seeded("fraude-1", titles={"fraude-1": "PF prende por fraude"})
    articles = FakeArticles()
    articles.add_ok("fraude-1", page_html("Curto.", paragraphs=1))

    run(store, articles)

    assert store.get_content("h-fraude-1").status == "unusable"  # type: ignore[union-attr]


def test_veredito_indeciso_deixa_a_url_pendente_e_a_proxima_nao_baixa_de_novo():
    store = seeded("mudo-1")
    articles = FakeArticles()
    articles.add_ok("mudo-1", page_html(QUIET, "Titulo mudo"))
    analyzer = FakeAnalyzer()

    first = run(store, articles, analyzer)

    assert store.get_analysis("h-mudo-1") is None
    assert first.totals.analyzed == 0
    assert len(store.pending_enrichment()) == 1

    run(store, articles, analyzer)

    assert articles.calls.count("https://a.com/mudo-1") == 1  # texto reaproveitado do estado
    assert len(analyzer.items) == 2
    assert analyzer.items[1].text != ""


def test_url_sem_manchete_nem_texto_vira_sem_achado_sem_chamar_o_modelo():
    store = seeded("2255890")
    articles = FakeArticles()
    articles.add_status("2255890", PageStatus.BLOCKED, 403)
    analyzer = FakeAnalyzer()

    run(store, articles, analyzer)

    assert analyzer.items == []
    analysis = store.get_analysis("h-2255890")
    assert analysis is not None
    assert (analysis.finding, analysis.basis) == (False, "headline")


def test_evidencia_pendente_de_rodada_anterior_e_retomada_baixando_de_novo_o_html():
    store = seeded("fraude-1")
    articles = FakeArticles()
    articles.add_ok("fraude-1", page_html(TEXT, "PF prende empresário"))

    class FailsOnce(FakeCapturer):
        def capture(self, finding, direct, *, expected_headline):
            record = super().capture(finding, direct, expected_headline=expected_headline)
            if len(self.calls) == 1:
                return EvidenceRecord(
                    finding.url_hash,
                    "failed",
                    None,
                    None,
                    None,
                    0,
                    None,
                    None,
                    None,
                    ("thin_content",),
                    "x",
                )
            return record

    capturer = FailsOnce()
    run(store, articles, capturer=capturer)
    assert store.get_evidence("h-fraude-1").status == "failed"  # type: ignore[union-attr]
    calls_before = articles.calls.count("https://a.com/fraude-1")

    summary = run(store, articles, capturer=capturer)

    assert store.get_evidence("h-fraude-1").status == "captured"  # type: ignore[union-attr]
    assert articles.calls.count("https://a.com/fraude-1") == calls_before + 1
    assert capturer.calls[1][1] is not None  # o HTML baixado de novo foi oferecido ao capturador
    assert summary.totals.evidence_reused == 1


def test_modo_somente_bright_data_nao_oferece_o_html_direto_ao_capturador():
    store = seeded("fraude-1")
    articles = FakeArticles()
    articles.add_ok("fraude-1", page_html(TEXT, "PF prende empresário"))
    capturer = FakeCapturer(provider="brightdata")

    summary = run(store, articles, capturer=capturer, evidence_mode="brightdata")

    assert capturer.calls[0][1] is None
    assert summary.totals.evidence_brightdata == 1


def test_evidencia_que_falha_e_contada_e_gravada_com_o_motivo():
    store = seeded("fraude-1")
    articles = FakeArticles()
    articles.add_ok("fraude-1", page_html(TEXT, "PF prende empresário"))

    class AlwaysFails(FakeCapturer):
        def capture(self, finding, direct, *, expected_headline):
            return EvidenceRecord(
                finding.url_hash,
                "failed",
                None,
                None,
                None,
                0,
                None,
                None,
                None,
                ("paywall",),
                "brightdata: paywall",
            )

    summary = run(store, articles, capturer=AlwaysFails())

    assert summary.totals.evidence_failed == 1
    saved = store.get_evidence("h-fraude-1")
    assert saved is not None and saved.rejected_reasons == ("paywall",)


def test_processa_em_blocos_e_respeita_o_limite():
    store = seeded(*(f"fraude-{n}" for n in range(1, 6)))
    articles = FakeArticles()
    for n in range(1, 6):
        articles.add_ok(f"fraude-{n}", page_html(TEXT, f"Titulo {n}"))
    analyzer = FakeAnalyzer()

    summary = run(store, articles, analyzer, chunk_size=2, batch_size=10, limit=4)

    assert analyzer.batches == [2, 2]  # dois blocos de 2, cada um em um lote
    assert summary.totals.urls == 4
    assert len(store.pending_enrichment()) == 1


def test_consumo_e_a_diferenca_de_antes_e_depois_inclusive_bright_data():
    store = seeded("fraude-1")
    articles = FakeArticles()
    articles.add_ok("fraude-1", page_html(TEXT, "PF prende empresário"))
    usages = iter(
        [
            Usage(calls=1, prompt_tokens=100, cost_usd=0.001),
            Usage(calls=3, prompt_tokens=1100, completion_tokens=300, cost_usd=0.0035, retries=1),
        ]
    )
    bd = iter([4, 6])

    totals = run(
        store, articles, analyzer_usage=lambda: next(usages), bright_data_requests=lambda: next(bd)
    ).totals

    assert (totals.prompt_tokens, totals.completion_tokens, totals.retries) == (1000, 300, 1)
    assert totals.cost_usd is not None and abs(totals.cost_usd - 0.0025) < 1e-9
    assert totals.bright_data_requests == 2


def test_execucao_fica_registrada_com_modelo_versao_e_modo():
    store = seeded("fraude-1")
    articles = FakeArticles()
    articles.add_ok("fraude-1", page_html(TEXT, "PF prende empresário"))

    summary = run(
        store, articles, model="google/x", prompt_version="ctx-v9", evidence_mode="brightdata"
    )

    [saved] = store.enrich_runs()
    assert (saved.id, saved.model, saved.prompt_version, saved.evidence_mode) == (
        summary.run_id,
        "google/x",
        "ctx-v9",
        "brightdata",
    )
    assert saved.totals == summary.totals
    assert saved.finished_at == NOW


def test_sem_pendentes_nada_e_chamado_e_a_execucao_e_registrada():
    store = StateStore()
    articles = FakeArticles()
    analyzer = FakeAnalyzer()

    summary = run(store, articles, analyzer)

    assert articles.calls == [] and analyzer.items == []
    assert summary.totals.urls == 0
    assert len(store.enrich_runs()) == 1


def test_url_nao_mantida_pela_triagem_nao_e_processada():
    store = StateStore()
    run_id = store.start_run(NOW)
    store.record_urls([found("fraude-1")], run_id=run_id, now=NOW, kind=UrlKind.NEW)  # sem veredito
    articles = FakeArticles()

    summary = run(store, articles)

    assert summary.totals.urls == 0
    assert articles.calls == []


def test_materia_neutra_com_entidade_recebe_evidencia_direta_sem_baixar_de_novo():
    store = seeded("vini-1", "fraude-2")
    articles = FakeArticles()
    articles.add_ok("vini-1", page_html(NEUTRAL, "Vinícius Júnior termina namoro"))
    articles.add_ok("fraude-2", page_html(TEXT, "PF prende empresário"))
    capturer = FakeCapturer()

    summary = run(store, articles, capturer=capturer)

    by_hash = {call[0].url_hash: call for call in capturer.calls}
    assert set(by_hash) == {"h-vini-1", "h-fraude-2"}
    assert by_hash["h-vini-1"][0].adverse is False
    assert by_hash["h-vini-1"][0].entity_names == ("Vinícius Júnior",)
    assert by_hash["h-fraude-2"][0].adverse is True
    assert by_hash["h-vini-1"][1] is not None  # o HTML da análise foi reaproveitado
    assert articles.calls.count("https://a.com/vini-1") == 1  # sem segundo download
    assert summary.totals.findings == 1  # "achado" continua sendo só o adverso
    assert summary.totals.evidence_reused == 2


def test_materia_sem_entidade_nao_gera_evidencia():
    store = seeded("praca-1")
    articles = FakeArticles()
    articles.add_ok("praca-1", page_html(QUIET, "Prefeito inaugura praça"))
    capturer = FakeCapturer()

    run(store, articles, capturer=capturer)

    assert capturer.calls == []


def test_evidencia_e_capturada_a_cada_bloco_e_nao_se_acumula_html_ate_o_fim():
    store = seeded("vini-1", "vini-2", "vini-3")
    articles = FakeArticles()
    for n in (1, 2, 3):
        articles.add_ok(f"vini-{n}", page_html(NEUTRAL, f"Vinícius Júnior {n}"))
    events: list[str] = []

    class Analyzer(FakeAnalyzer):
        def analyze(self, items):
            events.append("analisa")
            return super().analyze(items)

    class Capturer(FakeCapturer):
        def capture(self, finding, direct, *, expected_headline):
            events.append("captura")
            return super().capture(finding, direct, expected_headline=expected_headline)

    run(store, articles, analyzer=Analyzer(), capturer=Capturer(), chunk_size=1, batch_size=1)

    assert events == ["analisa", "captura"] * 3


def test_falha_de_evidencia_nao_e_retentada_nos_blocos_seguintes_da_mesma_execucao():
    store = seeded("vini-1", "vini-2", "vini-3")
    articles = FakeArticles()
    for n in (1, 2, 3):
        articles.add_ok(f"vini-{n}", page_html(NEUTRAL, f"Vinícius Júnior {n}"))
    attempts: dict[str, int] = {}

    class AlwaysFails(FakeCapturer):
        def capture(self, finding, direct, *, expected_headline):
            attempts[finding.url_hash] = attempts.get(finding.url_hash, 0) + 1
            return EvidenceRecord(
                finding.url_hash, "failed", None, None, None, 0, None, None, None, ("x",), "y"
            )

    run(store, articles, capturer=AlwaysFails(), chunk_size=1, batch_size=1)

    assert attempts == {"h-vini-1": 1, "h-vini-2": 1, "h-vini-3": 1}


def test_limite_de_evidencias_vale_para_a_execucao_toda():
    store = seeded("vini-1", "vini-2", "vini-3")
    articles = FakeArticles()
    for n in (1, 2, 3):
        articles.add_ok(f"vini-{n}", page_html(NEUTRAL, f"Vinícius Júnior {n}"))
    capturer = FakeCapturer()

    run(store, articles, capturer=capturer, chunk_size=1, batch_size=1, evidence_limit=2)

    assert len(capturer.calls) == 2
