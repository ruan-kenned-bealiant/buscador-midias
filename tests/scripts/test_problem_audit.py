"""A auditoria dirigida termina mesmo com índices cíclicos e feeds sem texto útil."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from buscador_midias.net.fetcher import FetchResult


def load(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("audit_problem_sources")


def test_orcamento_interrompe_antes_de_nova_requisicao(monkeypatch, tmp_path):
    module = load(monkeypatch)
    monkeypatch.setattr(module.samples, "OUTPUT", tmp_path)
    with httpx.Client() as client:
        reader = module.BudgetReader(client)
        reader.requests = 60
        assert reader.raw("https://example.com/feed").error == "audit_budget_reached"
        reader.requests = 0
        reader.started -= 181
        assert reader.raw("https://example.com/feed").error == "audit_budget_reached"


def test_indices_ciclicos_e_amostras_nao_criam_fila_infinita(monkeypatch, tmp_path):
    module = load(monkeypatch)
    monkeypatch.setattr(module, "OUT", tmp_path)
    (tmp_path / "resultados").mkdir()
    (tmp_path / "fontes").mkdir()
    requested = []

    class Reader:
        def __init__(self, client):
            self.started = module.time.monotonic()
            self.requests = 0
            self.cache_hits = 0
            self.policies = {}

        def allowed(self, site, url):
            return True

        def fetch(self, site, url):
            requested.append(url)
            self.requests += 1
            if url.endswith("/"):
                body = b"<html></html>"
            elif "/index" in url:
                number = int(url.rsplit("index", 1)[1])
                body = (
                    "<sitemapindex><sitemap><loc>https://example.com/index"
                    + str(number + 1)
                    + "</loc></sitemap><sitemap><loc>"
                    + url
                    + "</loc></sitemap></sitemapindex>"
                ).encode()
            elif "/feed" in url:
                body = (
                    "<rss><channel>"
                    + "".join(
                        f"<item><link>https://example.com/article{i}</link></item>"
                        for i in range(20)
                    )
                    + "</channel></rss>"
                ).encode()
            else:
                body = b"Not an article"
            return FetchResult(url, 200, body, None, url, "text/xml")

    monkeypatch.setattr(module, "BudgetReader", Reader)
    monkeypatch.setattr(module, "audit_page", lambda *a, **kw: dict(texto_util_confirmado=False))
    job = dict(
        base="https://example.com",
        seeds=["https://example.com/index0", "https://example.com/feed"]
        + [f"https://example.com/other{i}" for i in range(30)],
        before=dict(functional=0),
    )
    result = module.audit(
        job, SimpleNamespace(key="example.com", name="Example", owns=lambda u: True)
    )
    assert result["xml_tested"] == 12
    assert not any("/index3" in u for u in requested)
    assert len([u for u in requested if "/article" in u]) == 4
    assert result["remaining_not_executed"] > 0
    # Repetir o comando reutiliza o checkpoint e não faz novas chamadas.
    before = len(requested)
    assert (
        module.audit(job, SimpleNamespace(key="example.com", name="Example", owns=lambda u: True))
        == result
    )
    assert len(requested) == before
    assert json.loads((tmp_path / "fontes/example.com.json").read_text())["functional"] > 0


def test_grupos_restantes_sao_disjuntos_e_excluem_fontes_ja_auditadas(monkeypatch, tmp_path):
    module = load(monkeypatch)
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "plano.json").write_text(json.dumps(dict(jobs=[dict(key="done")])))
    monkeypatch.setattr(module, "PRIOR_AUDITS", (prior,))

    def source(key, functional):
        return dict(key=key, functional=functional)

    snapshot = dict(
        sources=[
            source("done", 0),
            source("missing", 0),
            source("blocked", 1),
            source("pending", 1),
            source("covered", 1),
        ],
        mechanisms={
            "blocked": [
                dict(article_blocked=True, texts=0, index=False, listed=10, url="https://b/rss")
            ],
            "pending": [
                dict(article_blocked=False, texts=1, index=False, listed=10, url="https://p/rss")
            ],
            "covered": [
                dict(article_blocked=False, texts=1, index=False, listed=10, url="https://c/rss")
            ],
        },
    )
    rows = [
        dict(
            fonte="pending",
            classificacao="inconclusivo",
            http_no_escopo="True",
            textos_confirmados="0",
            mecanismo="https://p/sitemap",
        )
    ]
    jobs = module.remaining_jobs(snapshot, rows)
    assert [(j["key"], j["group"]) for j in jobs] == [
        ("missing", 1),
        ("blocked", 2),
        ("pending", 3),
    ]


def test_falha_em_uma_fonte_fica_explicita_e_nao_aborta_o_lote(monkeypatch, tmp_path):
    module = load(monkeypatch)
    monkeypatch.setattr(module, "OUT", tmp_path)
    (tmp_path / "fontes").mkdir()

    def fail(*args):
        raise ValueError("resposta inesperada")

    monkeypatch.setattr(module, "audit", fail)
    result = module.safe_audit(
        dict(before=dict(functional=0), seeds=["https://x/rss"]),
        SimpleNamespace(key="x/news", name="X"),
    )
    assert result["incomplete"] and result["functional"] == 0
    assert "resposta inesperada" in result["home_error"]
    assert (tmp_path / "fontes/x__news.json").exists()


def test_reprocessamento_gzip_preserva_original_e_nao_acessa_rede(monkeypatch, tmp_path):
    import gzip
    import hashlib

    load(monkeypatch)
    module = importlib.import_module("repair_internal_audit")
    for name in ("fontes", "resultados", "http-cache"):
        (tmp_path / name).mkdir()
    url = "https://x.com/index.xml.gz"
    record = dict(
        url=url,
        final_url=url,
        http_status=200,
        xml_valido=False,
        tipo="",
        detalhe="decodificação inválida",
        amostras=[],
    )
    source = dict(key="x.com", records=[record], functional=0)
    path = tmp_path / "fontes/x.com.json"
    path.write_text(json.dumps(source))
    result_file = tmp_path / "resultados" / (module.samples.identity("x.com", url) + ".json")
    result_file.write_text(json.dumps(record))
    cache = tmp_path / "http-cache" / (hashlib.sha256(url.encode()).hexdigest() + ".gz")
    cache.write_bytes(
        gzip.compress(gzip.compress(b"<urlset><url><loc>https://x.com/a</loc></url></urlset>"))
    )
    original = result_file.read_bytes()
    assert module.repair_compressed(tmp_path) == [url]
    updated = json.loads(path.read_text())
    assert updated["functional"] == 1 and updated["records"][0]["reprocessado_offline"]
    assert (tmp_path / "correcoes-parser-originais" / result_file.name).read_bytes() == original
    assert module.repair_compressed(tmp_path) == [url]
    assert json.loads((tmp_path / "correcoes-parser.json").read_text())["network_requests"] == 0


def test_prova_textual_respeita_secao_e_alias_oficial_exato(monkeypatch):
    load(monkeypatch)
    module = importlib.import_module("export_functional_sources")
    site = SimpleNamespace(owns=lambda u: u.startswith("https://x.com/news/"))
    assert module.eligible_sample(
        site, dict(url="https://x.com/news/a", texto_util_confirmado=True)
    )
    assert not module.eligible_sample(
        site, dict(url="https://x.com/other/a", texto_util_confirmado=True)
    )
    assert module.eligible_sample(
        site, dict(url="https://official.example/a", texto_util_confirmado=True), "official.example"
    )
    assert not module.eligible_sample(
        site,
        dict(url="https://official.example.evil/a", texto_util_confirmado=True),
        "official.example",
    )
    assert not module.eligible_sample(
        SimpleNamespace(owns=lambda u: True),
        dict(url="https://www.bbc.com/audio/play/a", texto_util_confirmado=True),
    )
