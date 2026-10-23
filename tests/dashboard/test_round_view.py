import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from buscador_midias.dashboard.round_view import round_links
from buscador_midias.dashboard.server import make_server
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.state.store import SiteRunRecord, StateStore, UrlKind

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def found(path, stamp, *, published=True):
    return DiscoveredUrl('https://a.com/2026/10/19/' + path, 'hash-' + path, 'a.com',
                         'https://a.com/sitemap.xml', None if published else stamp,
                         '<img src=x onerror=alert(1)> ' + path, stamp if published else None)


@pytest.fixture
def round_server(tmp_path):
    directory = tmp_path / 'round'
    directory.mkdir()
    (directory / 'configuracao.json').write_text(
        json.dumps({'window_reference_at': NOW.isoformat()}))
    (directory / 'execucao.json').write_text(json.dumps({
        'run_id': 12, 'status': 'running', 'updated_at': NOW.isoformat(), 'sites_expected': 3,
    }))
    catalog = tmp_path / 'sites.csv'
    catalog.write_text('region,name,url,country,country_code,language,source_type\n'
                       'Brasil,A,https://a.com,Brasil,BR,Português,Jornal\n'
                       'Brasil,B,https://b.com,Brasil,BR,Português,Jornal\n'
                       'Brasil,C,https://c.com,Brasil,BR,Português,Jornal\n')
    store = StateStore()
    for _ in range(12):
        store.start_run(NOW)
    store.record_urls([found('outra-rodada', NOW)], run_id=1, now=NOW, kind=UrlKind.NEW)
    store.record_urls([found('recente', NOW - timedelta(hours=1))], run_id=12, now=NOW,
                      kind=UrlKind.NEW)
    store.record_urls([found('limite', NOW - timedelta(days=2), published=False)],
                      run_id=12, now=NOW, kind=UrlKind.BASELINE)
    store.record_urls([found('antiga', NOW - timedelta(days=2, seconds=1)),
                       found('sem-data', None), found('futura', NOW + timedelta(seconds=1))],
                      run_id=12, now=NOW, kind=UrlKind.NEW)
    store.record_urls([found('descartada', NOW)], run_id=12, now=NOW, kind=UrlKind.FILTERED)
    store.record_site_run(12, SiteRunRecord('a.com', 'ok', 1, 1, 0, 2, 1, 0, ''))
    store.record_site_run(12, SiteRunRecord('b.com', 'ok', 0, 0, 0, 0, 1, 0,
                                          'acessível, sem notícias recentes'))
    store.close()
    server = make_server(lambda: StateStore(read_only=True), '127.0.0.1', 0,
                         clock=lambda: NOW, sites_path=catalog, round_directory=directory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}', directory
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.read()


def test_tela_simples_e_resumo_isolam_rodada_12(round_server):
    base, _ = round_server
    status, body = get(base + '/rodada-12')
    assert status == 410
    assert b'OpenRouter' not in body and b'cost_total' not in body
    status, body = get(base + '/api/rodada-12')
    data = json.loads(body)
    assert status == 200 and data['state'] == 'running'
    assert data['completed'] == data['accessible'] == data['links'] == 2
    assert data['total'] == 3 and len(data['sources']) == 3
    assert {s['key']: s['category'] for s in data['sources']} == {
        'a.com': 'results', 'b.com': 'empty', 'c.com': 'pending',
    }
    assert 'enrich' not in data and 'cost_total_usd' not in data


def test_links_filtram_rodada_janela_datas_futuras_e_descartes(round_server):
    base, _ = round_server
    status, body = get(base + '/api/rodada-12/noticias?run_id=1')
    data = json.loads(body)
    assert status == 200
    assert {r['url'].rsplit('/', 1)[-1] for r in data['items']} == {'recente', 'limite'}
    assert {r['date_basis'] for r in data['items']} == {'publication', 'modification'}
    assert data['items'][0]['title'].startswith('<img')  # A tela usa textContent.
    _, body = get(base + '/api/rodada-12/noticias?site=b.com')
    assert json.loads(body)['items'] == []
    assert get(base + '/api/rodada-12/noticias?site=externo.com')[0] == 404
    assert get(base + '/api/rodada-12/noticias?before=invalid')[0] == 400
    assert get(base + '/api/rodada-12/noticias?before=-1')[0] == 400


def test_encerramento_parcial_preserva_fonte_incompleta_sem_simular_conclusao(round_server):
    base, directory = round_server
    store = StateStore()
    store.finish_run(12, NOW)
    store.close()
    (directory / 'encerramento.json').write_text(json.dumps({
        'run_id': 12, 'partial': True, 'documents_queued': 34,
    }))
    _, body = get(base + '/api/rodada-12')
    data = json.loads(body)
    assert data['state'] == 'partial'
    assert data['completed'] == 2 and data['total'] == 3
    assert data['documents_remaining'] == 34
    source = next(s for s in data['sources'] if s['key'] == 'c.com')
    assert source['category'] == 'pending'
    assert 'encerrada' in source['detail']


def test_paginacao_por_cursor_nao_repete_links(round_server):
    _, directory = round_server
    store = StateStore(read_only=True)
    first = round_links(store, directory, limit=1)
    assert first['next_cursor'] is not None
    second = round_links(store, directory, before=first['next_cursor'], limit=1)
    assert len(first['items']) == len(second['items']) == 1
    assert first['items'][0]['id'] != second['items'][0]['id']
    assert second['next_cursor'] is None
    store.close()
