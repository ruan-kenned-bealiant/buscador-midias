import threading
import time

import pytest

from buscador_midias.corpus.index import (
    CorpusConfigError,
    PostgresCorpusIndex,
    SynchronizedCorpusIndex,
    find_known_urls,
)
from buscador_midias.domain.url import legacy_url_hash, url_hash


class FakeCursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str]]:
        return self._rows


class FakeConnection:
    """Imita o trecho de psycopg.Connection que o índice usa."""

    def __init__(self, stored: set[str]) -> None:
        self.stored = stored
        self.queries: list[tuple[str, list[str]]] = []
        self.closed = False

    def execute(self, sql: str, params: tuple[list[str]]) -> FakeCursor:
        hashes = params[0]
        self.queries.append((sql, hashes))
        return FakeCursor([(h,) for h in hashes if h in self.stored])

    def close(self) -> None:
        self.closed = True


def test_known_hashes_devolve_so_os_que_existem():
    index = PostgresCorpusIndex(FakeConnection({"a", "c"}))

    assert index.known_hashes(["a", "b", "c"]) == {"a", "c"}


def test_consulta_usa_any_com_lista_e_a_tabela_do_corpus():
    connection = FakeConnection(set())

    PostgresCorpusIndex(connection).known_hashes(["a", "b"])

    sql, hashes = connection.queries[0]
    assert "midias_ia_articles" in sql
    assert "url_hash = ANY(%s)" in sql
    assert hashes == ["a", "b"]


def test_entrada_vazia_nao_consulta_o_banco():
    connection = FakeConnection(set())

    assert PostgresCorpusIndex(connection).known_hashes([]) == set()
    assert connection.queries == []


def test_divide_em_lotes_para_nao_estourar_o_limite_de_parametros():
    connection = FakeConnection(set())
    hashes = [f"h{i}" for i in range(2500)]

    PostgresCorpusIndex(connection, chunk_size=1000).known_hashes(hashes)

    assert [len(q[1]) for q in connection.queries] == [1000, 1000, 500]


def test_close_fecha_a_conexao():
    connection = FakeConnection(set())

    PostgresCorpusIndex(connection).close()

    assert connection.closed is True


class SetIndex:
    def __init__(self, known: set[str]) -> None:
        self.known = known

    def known_hashes(self, hashes: list[str]) -> set[str]:
        return {h for h in hashes if h in self.known}


def test_find_known_urls_reconhece_pelo_hash_atual():
    url = "https://noticia.com/artigo"

    assert find_known_urls([url], SetIndex({url_hash(url)})) == frozenset({url})


def test_find_known_urls_reconhece_linha_antiga_pelo_hash_legado():
    url = "https://www1.folha.uol.com.br/mundo/2026/08/materia.shtml"
    legacy = legacy_url_hash(url)
    assert legacy is not None

    assert find_known_urls([url], SetIndex({legacy})) == frozenset({url})
    assert find_known_urls([url], SetIndex({url_hash(url)})) == frozenset({url})


def test_find_known_urls_devolve_todas_as_variantes_que_colapsam_no_mesmo_hash():
    canonical = "https://a.com/x"
    variant = "http://www.a.com/x/?utm_source=newsletter"
    assert url_hash(canonical) == url_hash(variant)

    found = find_known_urls([canonical, variant], SetIndex({url_hash(canonical)}))

    assert found == frozenset({canonical, variant})


def test_find_known_urls_variantes_tambem_valem_para_o_hash_legado():
    first = "https://www1.folha.uol.com.br/mundo/a.shtml"
    second = "http://www1.folha.uol.com.br/mundo/a.shtml?utm_source=x"
    legacy = legacy_url_hash(first)
    assert legacy is not None
    assert legacy == legacy_url_hash(second)

    assert find_known_urls([first, second], SetIndex({legacy})) == frozenset({first, second})


def test_find_known_urls_ignora_o_que_o_corpus_nao_tem():
    known = "https://a.com/x"
    unknown = "https://a.com/y"

    assert find_known_urls([known, unknown], SetIndex({url_hash(known)})) == frozenset({known})


def test_find_known_urls_faz_uma_unica_consulta_agrupada():
    connection = FakeConnection(set())
    urls = ["https://a.com/1", "https://www1.a.com/2", "https://b.com/3"]

    find_known_urls(urls, PostgresCorpusIndex(connection))

    assert len(connection.queries) == 1
    assert len(connection.queries[0][1]) == 4  # 3 atuais + 1 legado


class OverlapDetector:
    """Falha se duas threads entrarem ao mesmo tempo em ``known_hashes``."""

    def __init__(self) -> None:
        self.inside = 0
        self.max_inside = 0
        self.guard = threading.Lock()

    def known_hashes(self, hashes: list[str]) -> set[str]:
        with self.guard:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
        time.sleep(0.002)
        with self.guard:
            self.inside -= 1
        return set()


def test_indice_sincronizado_serializa_consultas_de_varias_threads():
    detector = OverlapDetector()
    index = SynchronizedCorpusIndex(detector)

    threads = [
        threading.Thread(target=lambda: [index.known_hashes(["h"]) for _ in range(10)])
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert detector.max_inside == 1


def test_indice_sincronizado_repassa_o_resultado():
    index = SynchronizedCorpusIndex(SetIndex({"a"}))

    assert index.known_hashes(["a", "b"]) == {"a"}


ENV = {
    "DB_MIDIAS_IA_HOST": "db.interno",
    "DB_MIDIAS_IA_PORT": "5456",
    "DB_MIDIAS_IA_DATABASE": "power_dd_midias",
    "DB_MIDIAS_IA_USERNAME": "leitor",
    "DB_MIDIAS_IA_PASSWORD": "senha-secreta",
}


def test_from_env_conecta_somente_leitura_e_em_autocommit():
    seen: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> FakeConnection:
        seen.update(kwargs)
        return FakeConnection(set())

    PostgresCorpusIndex.from_env(ENV, connect=fake_connect)

    assert seen["host"] == "db.interno"
    assert seen["port"] == 5456
    assert seen["dbname"] == "power_dd_midias"
    assert seen["user"] == "leitor"
    assert seen["password"] == "senha-secreta"
    assert seen["options"] == "-c default_transaction_read_only=on"
    assert seen["autocommit"] is True


@pytest.mark.parametrize("missing", list(ENV))
def test_from_env_sem_variavel_nomeia_o_que_falta_sem_vazar_a_senha(missing: str):
    env = {k: v for k, v in ENV.items() if k != missing}

    with pytest.raises(CorpusConfigError) as error:
        PostgresCorpusIndex.from_env(env, connect=lambda **kw: FakeConnection(set()))

    assert missing in str(error.value)
    assert "senha-secreta" not in str(error.value)
