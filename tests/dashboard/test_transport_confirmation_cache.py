"""Provas compartilhadas têm fonte/perfil/URL e validade restritos, sem sessões importadas."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


def test_confirmation_cache_is_scoped_and_expires(tmp_path, monkeypatch):
    """Prova recente é compartilhada; outra fonte/perfil ou expiração exigem novo HTTP."""
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "recovery_cache_under_test", scripts / "recover_all_transports.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUT", tmp_path)
    calls = []
    root = SimpleNamespace(
        ok=True, status=200, error=None, content=b"xml", final_url=None, content_type="text/html"
    )

    class Reader:
        def __init__(self, *args, **kwargs):
            self.observations = []
            self.transport_events = []
            self.cache_hits = 0
            self.cooldown_deferred = False

        def get(self, url):
            return root

        def close(self):
            pass

    class Confirmation(Reader):
        def get(self, url):
            calls.append(url)
            return root

    monkeypatch.setattr(module, "CachedCurl", Reader)
    monkeypatch.setattr(module, "CurlReader", Confirmation)
    monkeypatch.setattr(
        module,
        "parse_document",
        lambda *args: dict(kind="feed", index=False, news=False, entries=["a"]),
    )
    monkeypatch.setattr(
        module,
        "run_test",
        lambda *args, **kwargs: dict(
            outcome="text_confirmed",
            usable_for_news=True,
            text_samples=1,
            samples=[dict(text=True, article=True, url="https://example.com/article")],
        ),
    )
    monkeypatch.setattr(
        module,
        "audit_page",
        lambda *args, **kwargs: dict(
            texto_util_confirmado=True, artigo_acessivel=True, audio_dedicado=False
        ),
    )
    monkeypatch.setattr(module, "preserve", lambda *args: None)
    models = {"rss": dict(parameters={}, version=1)}

    def check(ident, source=1, profile=None):
        return module.inspect_link(
            dict(
                id=ident,
                source_id=source,
                source_url="https://example.com",
                source_name="Example",
                url=f"https://example.com/rss/{ident}",
            ),
            models,
            profile,
        )

    first = check(1)
    assert first["confirmed"] and len(calls) == 1
    second = check(2)
    assert second["confirmed"] and len(calls) == 1
    assert second["confirmation"]["observed_at"] == first["confirmation"]["observed_at"]
    assert "reused_at" in second["confirmation"]
    assert check(3, source=2)["confirmed"] and len(calls) == 2
    assert check(4, profile="curl_chrome_h1")["confirmed"] and len(calls) == 3
    key = (1, "curl_chrome_h2", "https://example.com/article")
    module.CONFIRMATIONS[key]["observed_at"] = (
        datetime.now(UTC) - timedelta(minutes=6)
    ).isoformat()
    assert check(5)["confirmed"] and len(calls) == 4


def test_host_pause_has_finite_total_budget(tmp_path, monkeypatch):
    """Quatro links não fazem quatro pausas se o orçamento total permite somente três."""
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "recovery_pause_under_test", scripts / "recover_all_transports.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUT", tmp_path)
    monkeypatch.setattr(module.SESSIONS, "remaining", lambda host: 60)
    waits, inspected = [], []
    monkeypatch.setattr(module.time, "sleep", waits.append)

    def inspect(item, models, preferred):
        inspected.append(item["id"])
        return dict(confirmed=False, attempts=[])

    monkeypatch.setattr(module, "inspect_link", inspect)
    module.group([dict(id=i, url=f"https://example.com/rss/{i}") for i in range(4)], {})
    assert waits == [60, 60, 60]
    assert inspected == [0, 1, 2, 3]
    module.group([dict(id=i, url=f"https://example.com/rss/{i}") for i in range(4)], {})
    assert waits == [60, 60, 60]  # Retomar não reinicia o orçamento de pausas.
