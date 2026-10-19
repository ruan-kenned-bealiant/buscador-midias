from buscador_midias.discovery.document_queue import DocumentQueue


def test_alterna_fontes_e_nao_ocupa_worker_com_host_em_uso(tmp_path):
    queue = DocumentQueue(tmp_path / 'queue.db')
    queue.add_root('a', 'https://a.com/1.xml', 'sitemap')
    queue.add_root('a', 'https://a.com/2.xml', 'sitemap')
    queue.add_root('b', 'https://b.com/feed', 'feed')
    queue.commit()
    first = queue.take(set())
    assert first and first.site == 'a'
    second = queue.take({'a.com'})
    assert second and second.site == 'b'
    assert queue.take({'a.com', 'b.com'}) is None
    queue.finish(first, {'valid': True})
    third = queue.take({'b.com'})
    assert third and third.url.endswith('/2.xml')
    queue.close()


def test_checkpoint_reabre_so_ativas_e_nao_repete_documento_confirmado(tmp_path):
    path = tmp_path / 'queue.db'
    queue = DocumentQueue(path)
    root = queue.add_root('a', 'https://a.com/index.xml', 'sitemap')
    queue.commit()
    first = queue.take(set())
    assert first
    queue.finish(first, {'valid': True}, [('https://a.com/child.xml', 'sitemap')])
    second = queue.take(set())
    assert second and second.depth == 1
    queue.close()
    queue = DocumentQueue(path)
    resumed = queue.take(set())
    assert resumed and resumed.id == second.id
    queue.finish(resumed, {'valid': True}, [('https://a.com/index.xml#cycle', 'sitemap')])
    assert queue.take(set()) is None
    assert queue.db.execute('SELECT complete FROM roots WHERE id=?', (root,)).fetchone()[0] == 1
    assert queue.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    queue.close()
