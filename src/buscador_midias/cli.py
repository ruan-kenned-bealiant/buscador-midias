"""Comando ``dashboard``: o painel web local (as demais famílias têm CLI própria).

Famílias de comando de ``python -m buscador_midias``:

* ``db`` instala, importa e confere o cadastro (``buscador_midias.db.cli``);
* ``pipeline`` roda os serviços de busca e saúde (``buscador_midias.pipeline.service``);
* ``dashboard`` sobe o painel e a API local, aqui.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from http.server import ThreadingHTTPServer

from buscador_midias.dashboard.server import make_server


def build_parser() -> argparse.ArgumentParser:
    """Parser do comando; ``dashboard`` é o único subcomando deste módulo."""
    parser = argparse.ArgumentParser(
        prog="buscador_midias",
        description="Buscador contínuo de notícias para o corpus de Mídias IA.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    dashboard = commands.add_parser(
        "dashboard", help="Painel web local: rodadas, filas, saúde das fontes e cadastro."
    )
    dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        help="Endereço de escuta. Escritas só são aceitas do próprio computador.",
    )
    dashboard.add_argument("--port", type=int, default=8765, help="Porta (0 escolhe uma livre).")
    dashboard.add_argument(
        "--schema", default=None, help="Schema das tabelas bm_* (padrão: BM_SCHEMA)."
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    serve: Callable[[ThreadingHTTPServer], None] | None = None,
) -> int:
    """Executa o comando pedido.

    Args:
        argv: Argumentos (``None`` usa ``sys.argv``).
        serve: Como o painel atende as requisições (padrão: ``serve_forever``); os testes injetam
            uma função que não bloqueia.

    Returns:
        Código de saída do processo.
    """
    args = build_parser().parse_args(argv)
    server = make_server(args.host, args.port, schema=args.schema)
    print(
        f"painel em http://{args.host}:{server.server_address[1]}  (Ctrl+C para sair)", flush=True
    )
    try:
        (serve or (lambda srv: srv.serve_forever()))(server)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
