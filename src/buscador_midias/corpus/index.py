"""Índice somente leitura dos ``url_hash`` já presentes no corpus de Mídias IA (Postgres)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from buscador_midias.domain.url import url_hash_candidates

_QUERY = "SELECT url_hash FROM midias_ia_articles WHERE url_hash = ANY(%s)"
_READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
ENV_VARS = (
    "DB_MIDIAS_IA_HOST",
    "DB_MIDIAS_IA_PORT",
    "DB_MIDIAS_IA_DATABASE",
    "DB_MIDIAS_IA_USERNAME",
    "DB_MIDIAS_IA_PASSWORD",
)


class CorpusConfigError(ValueError):
    """Faltam variáveis de ambiente para conectar ao corpus."""


class CorpusIndex(Protocol):
    """Porta: responde quais hashes de URL o corpus já conhece."""

    def known_hashes(self, hashes: Sequence[str]) -> set[str]:
        """Devolve, dos hashes informados, os que existem no corpus."""
        ...


class PostgresCorpusIndex:
    """Consulta ``midias_ia_articles.url_hash`` no Postgres do corpus, sempre somente leitura."""

    def __init__(self, connection: Any, *, chunk_size: int = 1000) -> None:
        """Cria o índice sobre uma conexão já aberta.

        Args:
            connection: Conexão psycopg (ou equivalente com ``execute`` e ``close``).
            chunk_size: Máximo de hashes por consulta.
        """
        self._connection = connection
        self._chunk_size = chunk_size

    @classmethod
    def from_env(
        cls, env: Mapping[str, str], *, connect: Callable[..., Any] | None = None
    ) -> PostgresCorpusIndex:
        """Conecta usando as mesmas variáveis ``DB_MIDIAS_IA_*`` do KyX e do powerdd-backend.

        A sessão é aberta com ``default_transaction_read_only=on`` e em autocommit: nada é
        gravado mesmo que um bug tente.

        Args:
            env: Variáveis de ambiente.
            connect: Função de conexão (padrão: ``psycopg.connect``); injetável para testes.

        Raises:
            CorpusConfigError: Se faltar alguma variável (a mensagem nunca traz a senha).
        """
        missing = [name for name in ENV_VARS if not env.get(name)]
        if missing:
            raise CorpusConfigError(f"variáveis ausentes para o corpus: {', '.join(missing)}")

        if connect is None:
            # Import tardio: o driver só é necessário quando há corpus para consultar.
            import psycopg

            connect = psycopg.connect
        connection = connect(
            host=env["DB_MIDIAS_IA_HOST"],
            port=int(env["DB_MIDIAS_IA_PORT"]),
            dbname=env["DB_MIDIAS_IA_DATABASE"],
            user=env["DB_MIDIAS_IA_USERNAME"],
            password=env["DB_MIDIAS_IA_PASSWORD"],
            options=_READ_ONLY_OPTIONS,
            autocommit=True,
        )
        return cls(connection)

    def known_hashes(self, hashes: Sequence[str]) -> set[str]:
        """Dos hashes informados, quais existem no corpus (consulta em lotes)."""
        found: set[str] = set()
        for start in range(0, len(hashes), self._chunk_size):
            chunk = list(hashes[start : start + self._chunk_size])
            rows = self._connection.execute(_QUERY, (chunk,)).fetchall()
            found.update(row[0] for row in rows)
        return found

    def close(self) -> None:
        """Fecha a conexão."""
        self._connection.close()


class SynchronizedCorpusIndex:
    """Serializa as consultas de um índice, para uso seguro por várias threads.

    Uma conexão psycopg não deve ser compartilhada entre threads sem trava.
    """

    def __init__(self, inner: CorpusIndex) -> None:
        """Envolve um índice existente."""
        self._inner = inner
        self._lock = threading.Lock()

    def known_hashes(self, hashes: Sequence[str]) -> set[str]:
        """Consulta o índice interno, uma thread por vez."""
        with self._lock:
            return self._inner.known_hashes(hashes)


def find_known_urls(urls: Sequence[str], index: CorpusIndex) -> frozenset[str]:
    """URLs que o corpus já conhece, considerando o hash atual **e** o legado.

    Uma URL como ``www1.folha.uol.com.br/...`` gravada antes de 2026-10-01 está no corpus sob o
    hash antigo; sem conferir os dois, seria redescoberta como nova.

    Args:
        urls: URLs candidatas.
        index: Índice do corpus.

    Returns:
        O subconjunto de ``urls`` cujo hash atual ou legado existe no corpus (uma só consulta
        agrupada, em lotes).
    """
    # Várias URLs podem colapsar no mesmo hash (http/https, www, barra final, rastreio).
    owners: dict[str, list[str]] = {}
    for url in urls:
        for candidate in url_hash_candidates(url):
            owners.setdefault(candidate, []).append(url)
    known = index.known_hashes(list(owners))
    return frozenset(url for h in known for url in owners[h])
