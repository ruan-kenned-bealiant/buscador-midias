"""Política de saúde distingue acesso, ausência de notícias e formato inválido."""

import pytest

from buscador_midias.catalog.health_policy import classify


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"usable_for_news": True}, "healthy"),
        ({"outcome": "blocked", "endpoint_accessible": False}, "access_error"),
        ({"endpoint_accessible": False, "endpoint_status": 503}, "access_error"),
        ({"endpoint_accessible": False, "endpoint_error": "DNS failure"}, "access_error"),
        ({"xml_valid": True, "samples": [{"accessed": False, "status": 403}]}, "access_error"),
        ({"outcome": "empty", "endpoint_accessible": True}, "no_news"),
        ({"outcome": "audio_only", "samples": [{"accessed": True, "audio": True}]}, "no_text"),
        ({"outcome": "invalid", "endpoint_accessible": True}, "invalid"),
        (
            {"endpoint_accessible": False, "warnings": ["Orçamento de teste esgotado"]},
            "inconclusive",
        ),
        ({"endpoint_accessible": False, "warnings": ["HTTP 503"]}, "access_error"),
        (
            {"samples": [{"accessed": False, "reason": "Redirecionamento fora da fonte"}]},
            "inconclusive",
        ),
        ({}, "inconclusive"),
    ],
)
def test_classification(result, expected):
    assert classify(result) == expected
