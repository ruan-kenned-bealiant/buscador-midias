"""Fila durável por documento, com alternância de fontes e exclusão de hosts ocupados."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.domain.url import canonicalize_url


@dataclass(frozen=True, slots=True)
class DocumentTask:
    """Uma leitura de XML ou bootstrap; filhos viram outras tarefas, nunca leituras recursivas."""

    id: int
    root_id: int
    site: str
    url: str
    host: str
    kind: str
    depth: int


class DocumentQueue:
    """Persiste documentos antes de ler seus filhos e recupera tarefas interrompidas."""

    def __init__(self, path: Path) -> None:
        """Abre o checkpoint; somente o coordenador deve usar esta conexão."""
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS roots (
                id INTEGER PRIMARY KEY, site TEXT, url TEXT, kind TEXT,
                complete INTEGER NOT NULL DEFAULT 0, UNIQUE(site,url,kind)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, root_id INTEGER, site TEXT, url TEXT, host TEXT,
                kind TEXT, depth INTEGER, state TEXT NOT NULL DEFAULT 'queued', outcome TEXT,
                UNIQUE(root_id,url), FOREIGN KEY(root_id) REFERENCES roots(id)
            );
            CREATE INDEX IF NOT EXISTS tasks_source ON tasks(site,state,id);
            CREATE INDEX IF NOT EXISTS tasks_root ON tasks(root_id,state);
            CREATE INDEX IF NOT EXISTS roots_source ON roots(site,complete);
            CREATE TABLE IF NOT EXISTS evidence (
                root_id INTEGER, hash TEXT, url TEXT, title TEXT, date TEXT, basis TEXT,
                PRIMARY KEY(root_id,hash)
            );
        ''')
        self.db.execute("UPDATE tasks SET state='queued' WHERE state='active'")
        self.db.commit()
        self.rotation: deque[str] = deque()
        self.rotating: set[str] = set()
        for row in self.db.execute("SELECT DISTINCT site FROM tasks WHERE state='queued'"):
            self._activate(row['site'])

    def _activate(self, site: str) -> None:
        """Insere uma fonte uma única vez na rotação."""
        if site not in self.rotating:
            self.rotation.append(site)
            self.rotating.add(site)

    def add_root(self, site: str, url: str, kind: str) -> int:
        """Registra um mecanismo e sua primeira leitura, idempotentemente."""
        url = canonicalize_url(url)
        self.db.execute('INSERT OR IGNORE INTO roots(site,url,kind) VALUES (?,?,?)',
                        (site, url, kind))
        row = self.db.execute('SELECT id FROM roots WHERE site=? AND url=? AND kind=?',
                              (site, url, kind)).fetchone()
        root_id = int(row['id'])
        self._insert(root_id, site, url, kind, 0)
        self._activate(site)
        return root_id

    def _insert(self, root_id: int, site: str, url: str, kind: str, depth: int) -> None:
        """A identidade canônica por mecanismo evita ciclos e variantes de fragmento."""
        url = canonicalize_url(url)
        host = (urlsplit(url).hostname or '').removeprefix('www.')
        self.db.execute('''INSERT INTO tasks(root_id,site,url,host,kind,depth)
                           VALUES (?,?,?,?,?,?) ON CONFLICT(root_id,url) DO UPDATE SET
                           state=CASE WHEN excluded.depth < tasks.depth THEN 'queued'
                                      ELSE tasks.state END,
                           depth=min(tasks.depth,excluded.depth)''',
                        (root_id, site, url, host, kind, depth))

    def commit(self) -> None:
        """Fecha o lote inicial de mecanismos."""
        self.db.commit()

    def take(self, busy_hosts: set[str]) -> DocumentTask | None:
        """Entrega um documento de outra fonte sem ocupar worker esperando o mesmo host."""
        for _ in range(len(self.rotation)):
            site = self.rotation.popleft()
            self.rotating.remove(site)
            params: list[Any] = [site]
            restriction = ''
            if busy_hosts:
                restriction = ' AND host NOT IN (' + ','.join('?' for _ in busy_hosts) + ')'
                params.extend(sorted(busy_hosts))
            row = self.db.execute(
                "SELECT * FROM tasks WHERE site=? AND state='queued'" + restriction
                + ' ORDER BY id LIMIT 1', params,
            ).fetchone()
            if row is None:
                if self.db.execute("SELECT 1 FROM tasks WHERE site=? AND state='queued' LIMIT 1",
                                   (site,)).fetchone():
                    self._activate(site)
                continue
            self._activate(site)
            self.db.execute("UPDATE tasks SET state='active' WHERE id=?", (row['id'],))
            self.db.commit()
            return DocumentTask(*(row[key] for key in
                                  ('id', 'root_id', 'site', 'url', 'host', 'kind', 'depth')))
        return None

    def finish(
        self, task: DocumentTask, outcome: dict[str, Any],
        children: Sequence[tuple[str, str]] = (),
        evidence: Sequence[tuple[str, str, str, str, str]] = (),
    ) -> None:
        """Confirma resultado, evidências e novos documentos na mesma transação."""
        with self.db:
            for url, kind in children:
                self._insert(task.root_id, task.site, url, kind, task.depth + 1)
            self.db.executemany('INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?)',
                                [(task.root_id, *item) for item in evidence])
            self.db.execute("UPDATE tasks SET state='done',outcome=? WHERE id=?",
                            (json.dumps(outcome, ensure_ascii=False), task.id))
            pending = self.db.execute(
                "SELECT 1 FROM tasks WHERE root_id=? AND state!='done' LIMIT 1",
                (task.root_id,),
            ).fetchone()
            if not pending:
                self.db.execute('UPDATE roots SET complete=1 WHERE id=?', (task.root_id,))
        self._activate(task.site)

    def close(self) -> None:
        """Fecha o checkpoint."""
        self.db.close()
