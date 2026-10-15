from datetime import UTC, datetime

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import StateStore, UrlKind
from buscador_midias.triage.openrouter import Usage
from buscador_midias.triage.pipeline import triage_pending
from buscador_midias.triage.relevance import Decision, HeadlineItem, HeadlineVerdict

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def found(path: str, *, title: str | None = None, site: str = "a.com") -> DiscoveredUrl:
    return DiscoveredUrl(
        url=f"https://{site}/{path}",
        url_hash=f"hash-{site}-{path}",
        site_key=site,
        source_sitemap=f"https://{site}/sitemap.xml",
        lastmod=None,
        title=title,
        publication_date=None,
    )


def store_with(*urls: DiscoveredUrl) -> StateStore:
    store = StateStore()
    run = store.start_run(T0)
    store.record_urls(list(urls), run_id=run, now=T0, kind=UrlKind.NEW)
    return store


class FakeClassifier:
    """Decide pelo texto: 'clima' descarta, 'falha' fica desconhecido, o resto mantém."""

    def __init__(self) -> None:
        self.seen: list[HeadlineItem] = []

    def classify(self, items):
        self.seen.extend(items)
        out = []
        for item in items:
            if "clima" in item.text:
                out.append(HeadlineVerdict(item.id, Decision.DISCARD, (), "clima"))
            elif "falha" in item.text:
                out.append(HeadlineVerdict(item.id, Decision.UNKNOWN, (), "sem resposta"))
            else:
                out.append(HeadlineVerdict(item.id, Decision.KEEP, ("fiscal",), "fraude"))
        return out


def run(store: StateStore, classifier: FakeClassifier, **kwargs):
    return triage_pending(
        store, classifier, now=T0, model="m", prompt_version="v1", batch_size=2, workers=1, **kwargs
    )


def test_classifica_as_pendentes_e_grava_so_vereditos_definitivos():
    store = store_with(
        found("pf-prende-empresario-por-fraude"),
        found("previsao-do-clima-para-sexta"),
        found("manchete-com-falha-do-provedor"),
    )

    summary = run(store, FakeClassifier())

    assert (summary.headlines, summary.kept, summary.discarded, summary.pending) == (3, 1, 1, 1)
    assert store.triage_counts() == {"keep": 1, "discard": 1}
    assert [p.url for p in store.pending_triage()] == [
        "https://a.com/manchete-com-falha-do-provedor"
    ]


def test_o_modelo_recebe_titulo_ou_slug_e_o_dominio_da_fonte():
    store = store_with(
        found("x1", title="Titulo do sitemap", site="g1.globo.com"),
        found("pf-prende-empresario-por-fraude", site="itatiaia.com.br"),
    )
    classifier = FakeClassifier()

    run(store, classifier)

    assert [(i.domain, i.text) for i in classifier.seen] == [
        ("g1.globo.com", "Titulo do sitemap"),
        ("itatiaia.com.br", "pf prende empresario por fraude"),
    ]


def test_url_sem_manchete_e_mantida_sem_chamar_o_modelo():
    store = store_with(found("2255890"))
    classifier = FakeClassifier()

    summary = run(store, classifier)

    assert classifier.seen == []
    assert (summary.kept, summary.pending) == (1, 0)
    [row] = store.sample_triaged("keep", 5)
    assert row.reason == "sem manchete"


def test_segunda_execucao_retoma_so_o_que_ficou_pendente():
    store = store_with(found("pf-prende-empresario-por-fraude"), found("manchete-falha-aqui"))
    first = FakeClassifier()
    run(store, first)
    second = FakeClassifier()

    summary = run(store, second)

    assert [i.text for i in second.seen] == ["manchete falha aqui"]
    assert summary.headlines == 1


def test_sem_pendentes_nada_e_chamado_e_a_execucao_e_registrada():
    store = StateStore()
    classifier = FakeClassifier()

    summary = run(store, classifier)

    assert classifier.seen == []
    assert summary.headlines == 0
    assert len(store.triage_runs()) == 1


def test_execucao_grava_modelo_versao_do_prompt_e_consumo():
    store = store_with(found("pf-prende-empresario-por-fraude"))
    usages = iter(
        [Usage(), Usage(calls=1, prompt_tokens=400, completion_tokens=50, cost_usd=0.0002)]
    )

    summary = run(store, FakeClassifier(), usage_source=lambda: next(usages))

    [saved] = store.triage_runs()
    assert (saved.model, saved.prompt_version) == ("m", "v1")
    assert saved.totals.prompt_tokens == 400
    assert saved.totals.cost_usd == 0.0002
    assert summary.totals == saved.totals


def test_consumo_e_a_diferenca_entre_antes_e_depois():
    store = store_with(found("pf-prende-empresario-por-fraude"))
    usages = iter(
        [
            Usage(calls=5, prompt_tokens=1000, completion_tokens=100, cost_usd=0.01, retries=1),
            Usage(calls=6, prompt_tokens=1400, completion_tokens=150, cost_usd=0.0102, retries=3),
        ]
    )

    summary = run(store, FakeClassifier(), usage_source=lambda: next(usages))

    totals = summary.totals
    assert (totals.calls, totals.prompt_tokens, totals.completion_tokens) == (1, 400, 50)
    assert totals.retries == 2
    assert totals.cost_usd is not None
    assert abs(totals.cost_usd - 0.0002) < 1e-9


def test_limite_de_pendentes_por_execucao():
    store = store_with(*(found(f"materia-numero-{n}-sobre-fraude") for n in range(5)))

    summary = run(store, FakeClassifier(), limit=2)

    assert summary.headlines == 2
    assert len(store.pending_triage()) == 3
