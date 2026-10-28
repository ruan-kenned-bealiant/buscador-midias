"""Teto de chamadas pagas por site, somado entre todas as leituras daquele site."""

from __future__ import annotations

import httpx
import pytest

from buscador_midias.catalog.reader import TestReader
from buscador_midias.catalog.site_budget import SiteBudget, budgeted_reader
from buscador_midias.net.fetcher import FetchResult

URL = "https://public.example/feed/"
ROOTS = [URL]


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "fake-secret")
    monkeypatch.setenv("BRIGHTDATA_ZONE_UNLOCKER", "test-zone")
    monkeypatch.setattr("buscador_midias.catalog.bright_reader.public_ip", lambda *args: "8.8.8.8")
    monkeypatch.setattr(
        TestReader, "_fetch", lambda self, url, **kwargs: FetchResult(url, 403, b"", None, url)
    )
    monkeypatch.setattr(TestReader, "_allowed", lambda self, url: (True, ""))


def handler(request):
    return httpx.Response(
        200,
        json=dict(status_code=200, headers={"Content-Type": "application/xml"}, body="<rss/>"),
    )


def make(budget):
    return budgeted_reader(
        budget,
        ROOTS,
        lambda _: None,
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
        direct_pause=0,
    )


def test_the_budget_counts_down_and_never_goes_negative():
    budget = SiteBudget(2)
    assert budget.remaining == 2
    budget.spend(1)
    assert budget.remaining == 1
    budget.spend(5)
    assert budget.remaining == 0


def test_a_budget_cannot_start_negative():
    with pytest.raises(ValueError):
        SiteBudget(-1)


def test_two_paid_calls_per_site_even_across_different_readers(setup):
    budget = SiteBudget(2)
    first = make(budget)
    assert first.get(URL).ok
    assert first.get(URL + "a").ok
    third = first.get(URL + "b")
    assert not third.ok and first.bright_limit_reached
    second = make(budget)
    assert not second.get(URL + "c").ok
    assert budget.spent == 2


def test_the_spend_is_recorded_as_soon_as_it_happens(setup):
    budget = SiteBudget(2)
    reader = make(budget)
    reader.get(URL)
    assert budget.spent == 1 and budget.remaining == 1


def test_an_exhausted_budget_never_calls_the_provider(setup):
    budget = SiteBudget(0)
    reader = make(budget)
    assert not reader.get(URL).ok
    assert reader.bright_requests == 0 and budget.spent == 0


def test_readers_use_two_direct_attempts_before_paying(setup, monkeypatch):
    asked = []
    monkeypatch.setattr(
        TestReader,
        "_fetch",
        lambda self, url, **kw: asked.append(url) or FetchResult(url, 403, b"", None, url),
    )
    budget = SiteBudget(2)
    make(budget).get(URL)
    assert len(asked) == 2 and budget.spent == 1
