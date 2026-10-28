"""Links de um mesmo site que não trazem nada além do que os outros já trazem.

Feeds por tag, autor ou organização costumam ser recortes do feed principal: lê-los gasta tempo
de rodada e de saúde sem achar notícia nova. A cobertura é gulosa (sempre o link que acrescenta
mais artigos ainda não cobertos); o que sobra, por não acrescentar nenhum, é redundante.
"""

from __future__ import annotations


def redundant_links(articles: dict[str, set[str]]) -> list[str]:
    """Chaves cujos artigos já estão todos nas demais, na menor cobertura que cobre o conjunto.

    Links sem nenhum artigo não entram: nada prova que sobram. O desempate é determinístico
    (mais artigos primeiro, depois a chave em ordem alfabética).
    """
    pool = {key: items for key, items in articles.items() if items}
    covered: set[str] = set()
    kept: set[str] = set()
    remaining = dict(pool)
    while remaining:
        key = min(remaining, key=lambda k: (-len(remaining[k] - covered), k))
        if not remaining[key] - covered:
            break
        covered |= remaining.pop(key)
        kept.add(key)
    return sorted(set(pool) - kept)
