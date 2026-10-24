"""Classifica o teste real sem confundir falta de notícias com falha de acesso."""

from typing import Any


def classify(result: dict[str, Any]) -> str:
    """Usa acesso, artigo e texto; data de publicação não interfere na saúde."""
    if result.get("usable_for_news"):
        return "healthy"
    if result.get("bright_data_provider_error") or result.get("bright_data_limit_reached"):
        return "inconclusive"
    if result.get("outcome") == "blocked":
        return "access_error"
    if result.get("endpoint_accessible") is False and (
        result.get("endpoint_status") is not None or result.get("endpoint_error")
    ):
        return "access_error"
    samples = result.get("samples", [])
    if any(
        s.get("accessed") is False
        and (
            s.get("status") is not None
            or any(
                marker in str(s.get("reason", "")).lower()
                for marker in (
                    "timeout",
                    "timed out",
                    "connection",
                    "network",
                    "dns",
                    "ssl",
                    "http ",
                )
            )
        )
        for s in samples
    ):
        return "access_error"
    if result.get("outcome") == "empty":
        return "no_news"
    if result.get("outcome") in ("invalid", "incompatible"):
        return "invalid"
    if any(s.get("accessed") for s in samples):
        return "no_text"
    # Resultados anteriores não tinham status estruturado na raiz.
    if result.get("endpoint_accessible") is False and any(
        marker in str(result.get("warnings", [])).lower()
        for marker in (
            "http ",
            "timeout",
            "timed out",
            "connection",
            "network",
            "dns",
            "ssl",
            "robots_",
        )
    ):
        return "access_error"
    return "inconclusive"
