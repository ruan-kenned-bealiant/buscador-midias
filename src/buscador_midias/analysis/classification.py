"""Vocabulário fechado de conteúdo, independente do risco e do estado da fila."""

CONTENT_CATEGORIES = {
    "news": "Notícia / reportagem",
    "opinion": "Opinião / editorial / coluna opinativa",
    "weather": "Previsão do tempo",
    "cooking": "Culinária / receitas",
    "sports": "Esportes",
    "entertainment": "Entretenimento / cultura",
    "lifestyle": "Estilo de vida / dicas",
    "horoscope": "Horóscopo / astrologia",
}


def content_category(value: object) -> str | None:
    """Normaliza apenas categorias conhecidas; ausência ou valor inválido permanece nulo."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in CONTENT_CATEGORIES else None
