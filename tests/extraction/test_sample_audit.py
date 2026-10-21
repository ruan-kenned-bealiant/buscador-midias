import json
from datetime import UTC, datetime, timedelta

from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def sample(types, published='2026-10-18T12:00:00Z'):
    data = {'@context': 'https://schema.org', '@type': types,
            'headline': 'Reportagem sobre os acontecimentos na cidade', 'datePublished': published}
    paragraph = ('A reportagem ouviu moradores e autoridades sobre os acontecimentos na cidade. '
                 'Os documentos apresentados detalham os impactos das medidas anunciadas. ')
    body = ('<html><head><title>Reportagem sobre os acontecimentos na cidade</title>'
            '<script type="application/ld+json">' + json.dumps(data) + '</script></head>'
            '<body><article><h1>Reportagem sobre os acontecimentos na cidade</h1>'
            + ''.join(f'<p>{paragraph}</p>' for _ in range(15)) + '</article></body></html>')
    return PageSnapshot('https://example.com/noticia/acontecimentos-na-cidade', 200,
                        'text/html', None, body.encode())


def test_data_de_modificacao_do_feed_nao_promove_artigo_antigo_a_recente():
    result = audit_page(sample('NewsArticle', '2020-01-01T12:00:00Z'),
                        since=NOW-timedelta(days=3), until=NOW, now=NOW)
    assert result['texto_util_confirmado']
    assert result['publicacao_na_janela'] == 'nao'


def test_podcast_dedicado_nao_e_noticia_textual_mesmo_com_descricao_longa():
    result = audit_page(sample('PodcastEpisode'),
                        since=NOW-timedelta(days=3), until=NOW, now=NOW)
    assert result['categoria'] == 'audio_podcast'
    assert not result['texto_util_confirmado']


def test_artigo_com_audio_embutido_preserva_texto():
    result = audit_page(sample(['NewsArticle', 'AudioObject']),
                        since=NOW-timedelta(days=3), until=NOW, now=NOW)
    assert result['sinal_audio']
    assert not result['audio_dedicado']
    assert result['texto_util_confirmado']
    assert result['publicacao_na_janela'] == 'sim'


def test_mime_audio_nao_e_interpretado_como_html():
    page = PageSnapshot('https://example.com/arquivo.mp3', 200, 'audio/mpeg', None, b'ID3')
    result = audit_page(page, since=NOW-timedelta(days=3), until=NOW, now=NOW)
    assert result['audio_dedicado']
    assert not result['texto_util_confirmado']
