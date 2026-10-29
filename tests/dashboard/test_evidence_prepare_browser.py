"""O preparo da evidência roda no navegador; estes testes o executam num Chrome real."""

from __future__ import annotations

from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

SCRIPT = Path(__file__).parents[2] / "src/buscador_midias/dashboard/evidence_prepare.js"
PIXEL = '<noscript><img src="//metrics.example/p.gif" width="1" height="1"></noscript>'


@pytest.fixture(scope="module")
def prepare():
    """Carrega o módulo num Chrome e expõe ``prepareEvidenceHtml`` como função Python."""
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome")
        except sync_api.Error:
            pytest.skip("Chrome indisponível para os testes de navegador.")
        page = browser.new_page()
        page.set_content("<!doctype html><title>t</title>")
        page.evaluate(
            "async src => { window.mod = await import("
            "URL.createObjectURL(new Blob([src], {type: 'text/javascript'}))) }",
            SCRIPT.read_text(encoding="utf-8"),
        )
        yield lambda html: page.evaluate("html => window.mod.prepareEvidenceHtml(html)", html)
        browser.close()


def test_noscript_fallbacks_are_not_rendered(prepare):
    """O iframe não executa scripts, então o ``noscript`` apareceria e o CSS do site o esticaria.

    Um pixel de rastreio 1x1 vira um quadrado do tamanho da largura da página e empurra o
    conteúdo para baixo, deixando a primeira tela em branco.
    """
    html = f"<html><body>{PIXEL}<h1>Título</h1><noscript><p>ative o js</p></noscript></body></html>"

    result = prepare(html)["html"]

    assert "<noscript" not in result
    assert "<h1>Título</h1>" in result


def test_scripts_are_still_removed(prepare):
    """Nenhum script da página capturada pode chegar ao iframe."""
    result = prepare("<html><body><script>alert(1)</script><p>texto</p></body></html>")["html"]

    assert "<script" not in result
    assert "<p>texto</p>" in result
