"""Servidor local da tela de rotulagem: uma página, a lista cega e um endpoint de voto.

Diferente do painel de monitoramento (só leitura), este servidor **grava** no CSV. Por isso, além
de escutar só em ``127.0.0.1``, ele recusa o que não vier da própria página:

* ``Host`` precisa ser localhost (barra o *DNS rebinding*);
* ``Origin``, quando presente, precisa ser o próprio endereço;
* o voto só vale como ``application/json`` (um formulário de outro site não consegue enviar isso
  sem uma pré-checagem CORS, que este servidor não responde);
* o corpo tem tamanho máximo e o veredito é uma lista fechada.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from buscador_midias.labeling.session import LabelSession

MAX_BODY_BYTES = 1024
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
)


def make_label_server(folder: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Cria o servidor da tela de rotulagem (sem iniciá-lo).

    Args:
        folder: Pasta com os lotes ``rotulagem-NN.csv`` (a mesma de ``labels export``).
        host: Endereço de escuta (use ``127.0.0.1``: não há autenticação).
        port: Porta (``0`` escolhe uma livre).

    Returns:
        O servidor, pronto para ``serve_forever``.
    """
    session = LabelSession(folder)
    page = resources.files("buscador_midias.labeling").joinpath("index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        """Atende ``GET /``, ``GET /api/items`` e ``POST /api/verdict``."""

        server_version = "BuscadorRotulagem"

        def do_GET(self) -> None:  # noqa: N802 - nome exigido pelo http.server
            """Página e lista de itens."""
            if not self._local_request():
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif path == "/api/items":
                self._json(200, self._listing())
            else:
                self._json(404, {"error": "não encontrado"})

        def do_POST(self) -> None:  # noqa: N802 - nome exigido pelo http.server
            """Grava um voto."""
            if not self._local_request():
                return
            if urlsplit(self.path).path != "/api/verdict":
                self._json(404, {"error": "não encontrado"})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != (
                "application/json"
            ):
                self._json(415, {"error": "use application/json"})
                return
            payload = self._read_json()
            if payload is None:
                return
            label_id, verdict = payload.get("id"), payload.get("verdict")
            if not isinstance(label_id, str) or not isinstance(verdict, str):
                self._json(400, {"error": "esperado {id, verdict} em texto"})
                return
            try:
                session.set_verdict(label_id, verdict)
            except ValueError:
                self._json(400, {"error": "veredito inválido"})
            except KeyError:
                self._json(404, {"error": "item desconhecido"})
            else:
                listing = self._listing()
                self._json(200, {"ok": True, "done": listing["done"], "total": listing["total"]})

        def _method_not_allowed(self) -> None:
            """Recusa métodos fora de GET e POST."""
            self._json(405, {"error": "método não permitido"})

        do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815

        def _listing(self) -> dict[str, object]:
            """Itens (só campos cegos) e o progresso."""
            items = session.items()
            return {
                "items": [asdict(item) for item in items],
                "total": len(items),
                "done": sum(1 for item in items if item.verdict),
            }

        def _local_request(self) -> bool:
            """Confere ``Host`` e ``Origin``; se forem de fora, responde 403 e devolve ``False``."""
            host_header = self.headers.get("Host", "")
            hostname = urlsplit(f"//{host_header}").hostname or ""
            origin = self.headers.get("Origin")
            if hostname not in _LOCAL_HOSTS or (
                origin is not None and origin != f"http://{host_header}"
            ):
                self._json(403, {"error": "acesso recusado"})
                return False
            return True

        def _read_json(self) -> dict[str, object] | None:
            """Lê o corpo (limitado) como objeto JSON; responde o erro e devolve ``None``."""
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(411, {"error": "Content-Length obrigatório"})
                return None
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "corpo grande demais"})
                return None
            try:
                payload = json.loads(self.rfile.read(max(length, 0)))
            except ValueError:
                self._json(400, {"error": "JSON inválido"})
                return None
            if not isinstance(payload, dict):
                self._json(400, {"error": "esperado um objeto JSON"})
                return None
            return payload

        def _json(self, status: int, payload: dict[str, object]) -> None:
            """Responde com JSON."""
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            """Envia a resposta com cabeçalhos de segurança e sem cache."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Silencia o log por requisição."""

    return ThreadingHTTPServer((host, port), Handler)
