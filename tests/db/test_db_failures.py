"""Classificação do motivo de uma falha: código curto e classe que decide o retry."""

from __future__ import annotations

import pytest

from buscador_midias.db.failures import AUTO_RETRY_CLASSES, AUTO_RETRY_CODES, classify


@pytest.mark.parametrize(
    ("reason", "code", "klass"),
    [
        ("HTTP 401 no XML", "http_401", "blocked"),
        ("HTTP 403 no XML", "http_403", "blocked"),
        ("HTTP 402 no XML", "http_402", "blocked"),
        ("HTTP 404 no XML", "http_404", "permanent"),
        ("HTTP 410 no XML", "http_410", "permanent"),
        ("HTTP 429 no XML", "http_429", "transient"),
        ("HTTP 408 no XML", "http_408", "transient"),
        ("HTTP 500 no XML", "http_500", "transient"),
        ("HTTP 503 no XML", "http_503", "transient"),
        ("redirect_cycle", "redirect_cycle", "permanent"),
        ("URL fora dos domínios do site", "outside_domain", "permanent"),
        ("robots_unavailable", "robots_unavailable", "permanent"),
        ("bright_data_api_error: 500", "bright_data_error", "permanent"),
        ("timeout lendo o corpo", "timeout", "transient"),
        ("Read timed out", "timeout", "transient"),
        ("connection reset by peer", "connection", "transient"),
        ("Conexão encerrada", "connection", "transient"),
        ("XML inválido: no element found", "invalid_xml", "transient"),
        ("Mecanismo incompatível com o XML", "invalid_xml", "transient"),
        ("Falha interna: KeyError", "internal", "bug"),
        ("Resposta de análise vazia", "empty_analysis", "transient"),
        ("Filtro de conteúdo do provedor; análise recusada", "content_filtered", "permanent"),
        ("alguma coisa nunca vista", "other", "transient"),
    ],
)
def test_reason_maps_to_code_and_class(reason, code, klass):
    failure = classify(reason)
    assert (failure.code, failure.klass) == (code, klass)


def test_non_retryable_flag_overrides_a_transient_class():
    failure = classify("timeout lendo o corpo", retryable=False)
    assert failure.klass == "permanent" and failure.code == "timeout"


def test_blocked_and_permanent_stay_as_they_are_when_not_retryable():
    assert classify("HTTP 403 no XML", retryable=False).klass == "blocked"
    assert classify("HTTP 404 no XML", retryable=False).klass == "permanent"


def test_detail_keeps_the_original_text_truncated():
    failure = classify("x" * 900)
    assert len(failure.detail) == 500


def test_only_transient_failures_are_retried_automatically():
    assert AUTO_RETRY_CLASSES == ("transient",)


def test_a_403_is_also_retried_automatically_because_it_is_often_a_rate_limit():
    """401 é paywall e 402 pede pagamento: repetir não muda nada. O 403 muitas vezes é o limite de taxa."""
    assert AUTO_RETRY_CODES == ("http_403",)
    assert classify("HTTP 403 na notícia").klass == "blocked"  # continua aparecendo como bloqueio
