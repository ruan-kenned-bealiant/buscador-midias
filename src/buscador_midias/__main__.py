"""Permite executar com ``python -m buscador_midias``."""

import sys

from buscador_midias.cli import main

if sys.argv[1:2] == ["ingest"]:
    from buscador_midias.ingestion.runtime import main as ingest_main

    raise SystemExit(ingest_main(sys.argv[2:]))

raise SystemExit(main())
