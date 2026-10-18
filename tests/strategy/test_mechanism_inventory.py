import csv
import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.domain.site import Site
from buscador_midias.state.store import StateStore
from buscador_midias.strategy.discovery_plan import load_discovery_plan
from buscador_midias.strategy.hints import load_hints
from buscador_midias.strategy.manual import ManualRules, ManualRulesBook
from buscador_midias.strategy.mechanism_inventory import (
    MechanismIndex,
    export_mechanisms,
    mechanism_record,
)

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
SITE = Site.create(
    name="A",
    url="https://a.com/politica",
    region="",
    country="Brasil",
    country_code="BR",
    language="pt",
    source_type="portal",
)
ARTICLE = "https://a.com/politica/noticia-de-exemplo"


def source(url="https://a.com/rss", *, at=NOW, kind="feed", verdict="fresh", partial=False):
    return MapSource(url, verdict, kind, (ARTICLE,), at, partial=partial)


def record(url="https://a.com/rss"):
    return mechanism_record(
        SITE,
        dict(
            url=url,
            kind="feed",
            verdict="fresh",
            listed_count=10,
            partial=False,
            checked_at=NOW,
        ),
    )


def test_endereco_de_maquina_pode_ficar_fora_do_prefixo_editorial():
    assert record()["group"] == "recent"
    assert record()["functional"] is True
    assert record()["checked_at"] == NOW.isoformat()
    for url in (
        "https://outro.com/rss",
        "javascript:alert(1)",
        "https://a.com/rss?signature=segredo",
        "https://user:segredo@a.com/rss",
    ):
        assert record(url)["group"] == "excluded"
        assert record(url)["url"] is None
    dated = record("https://a.com/sitemap-2026-10-18.xml")
    assert dated["group"] == "review" and dated["functional"] and dated["fixed_date"]


def test_classifica_legibilidade_sem_prometer_atualizacao():
    for verdict, expected in [
        ("slow", "supplement"),
        ("undated", "supplement"),
        ("stale", "review"),
        ("archive", "review"),
        ("empty", "review"),
    ]:
        row = dict(
            url="https://a.com/rss",
            kind="feed",
            verdict=verdict,
            listed_count=2,
            checked_at=NOW,
            partial=False,
        )
        assert mechanism_record(SITE, row)["group"] == expected
        assert mechanism_record(SITE, row)["functional"] is True
    row["listed_count"] = 0
    row["verdict"] = "empty"
    assert mechanism_record(SITE, row)["functional"] is True
    row["verdict"] = "unavailable"
    assert mechanism_record(SITE, row)["functional"] is False
    row["kind"] = "html_pagination"
    assert mechanism_record(SITE, row)["group"] == "excluded"
    row["kind"] = "feed"
    assert mechanism_record(SITE, row, ["https://a.com/rss"])["group"] == "excluded"


def test_associacao_separa_declaracao_correspondencia_e_leitura_historica():
    page = MapPage(
        "https://a.com/politica",
        "",
        "section",
        PageStatus.OK,
        checked_at=NOW,
        article_urls=(ARTICLE,),
        feeds=("https://a.com/novo-rss",),
    )
    index = MechanismIndex(
        SITE,
        [
            source(at=NOW - timedelta(hours=1)),
            source("https://a.com/comments", kind="comments_feed"),
        ],
    )
    result = index.associate(page)
    assert result["status"] == "historical_match"
    assert result["matched"] == 0 and result["historical_matched"] == 1
    assert len(result["mechanisms"]) == 1
    assert result["unvalidated_declared"] == ["https://a.com/novo-rss"]
    result = MechanismIndex(SITE, [source(partial=True)]).associate(page)
    assert result["status"] == "sample_full"
    assert result["mechanisms"][0]["partial"] is True
    result = MechanismIndex(SITE, [source()], ["https://a.com/rss"]).associate(page)
    assert result["status"] == "no_match"


def test_exportacao_gera_plano_e_hints_consumiveis_sem_promover_estrategias(
    tmp_path: Path,
    state_schema,
):
    # A fixture autouse cria um schema t_* em buscador_midias_test e o remove ao terminar.
    assert state_schema and state_schema.startswith("t_")
    assert os.environ["DB_MIDIAS_IA_DATABASE"] == "buscador_midias_test"
    assert os.environ["BUSCADOR_STATE_SCHEMA"] == state_schema
    store = StateStore()
    store.save_mapping_pages(
        SITE.key,
        [
            MapPage(
                SITE.url,
                "",
                "section",
                PageStatus.OK,
                checked_at=NOW,
                article_urls=(ARTICLE,),
                feeds=("https://a.com/rss",),
            )
        ],
    )
    store.save_mapping_sources(
        SITE.key,
        [
            source(),
            source("https://a.com/excluido"),
            source("https://a.com/sitemap-2026-10-18.xml", kind="sitemap"),
            source("https://a.com/antigo", verdict="stale"),
            source("https://a.com/sem-data", verdict="undated"),
        ],
    )
    store.close()
    rules = ManualRulesBook({SITE.key: ManualRules(("https://a.com/excluido",), ())})
    store = StateStore(read_only=True)
    try:
        summary = export_mechanisms(store, [SITE], rules, tmp_path)
        assert store.list_runs() == []
        assert store.site_strategies(SITE.key) == []
    finally:
        store.close()
    plan = load_discovery_plan(tmp_path / "plano-descoberta.json", {SITE.key: SITE})
    assert [r.url for r in plan[SITE.key]] == ["https://a.com/rss"]
    assert load_hints(tmp_path / "hints-recentes.csv").for_site(SITE) == ("https://a.com/rss",)
    assert summary["plan_sources"] == 1 and summary["http_requests"] == 0
    functional = load_discovery_plan(tmp_path / "plano-funcional.json", {SITE.key: SITE})
    assert {r.url for r in functional[SITE.key]} == {
        "https://a.com/rss",
        "https://a.com/antigo",
        "https://a.com/sem-data",
        "https://a.com/sitemap-2026-10-18.xml",
    }
    assert summary["functional_plan_sources"] == 1
    assert summary["functional_plan_mechanisms"] == 4
    assert set(load_hints(tmp_path / "hints-funcionais.csv").for_site(SITE)) == {
        r.url for r in functional[SITE.key]
    }
    assert summary["mechanisms_supplement"] == 1
    expanded = load_discovery_plan(tmp_path / "plano-ampliado.json", {SITE.key: SITE})
    assert {r.url for r in expanded[SITE.key]} == {"https://a.com/rss", "https://a.com/sem-data"}
    assert load_hints(tmp_path / "hints-complementares.csv").for_site(SITE) == (
        "https://a.com/sem-data",
    )
    with gzip.open(tmp_path / "secoes-mecanismos.csv.gz", "rt") as handle:
        assert list(csv.DictReader(handle))[0]["mecanismo"] == "https://a.com/antigo"
    assert json.loads((tmp_path / "resumo.json").read_text())["sources"] == 1
