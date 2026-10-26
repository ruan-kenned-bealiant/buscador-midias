"""Provas de data e segregação editorial do teste finito de evidências."""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "priority_probe", SCRIPTS / "probe_priority_news_evidence.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize(
    "url",
    [
        "https://reuters.com/plus/advertiser/story",
        "https://www.washingtonpost.com/creativegroup/sitemaps/sitemap.xml",
        "https://valor.globo.com/patrocinado/noticia/2026/10/26/a.ghtml",
        "https://nytimes.com/cooking/recipe",
    ],
)
def test_comercial_nao_comprova_cobertura_editorial(url):
    assert not probe.editorial(url)


def test_data_utc_respeita_dia_no_brasil_e_nao_inventa_data():
    now = datetime(2026, 10, 26, 15, tzinfo=UTC)
    assert probe.publication_day("2026-10-26T01:00:00+00:00", now) == "anterior"
    assert probe.publication_day("2026-10-26T09:00:00-03:00", now) == "hoje"
    assert probe.publication_day("2026-10-27T09:00:00-03:00", now) == "desconhecida"
    assert probe.publication_day(None, now) == "desconhecida"
