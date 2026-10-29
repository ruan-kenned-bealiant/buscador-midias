"""Permite executar com ``python -m buscador_midias``."""

import sys

if sys.argv[1:2] == ["pipeline"]:
    from buscador_midias.pipeline.service import main as pipeline_main

    raise SystemExit(pipeline_main(sys.argv[2:]))

if sys.argv[1:2] == ["db"]:
    from buscador_midias.db.cli import main as db_main

    raise SystemExit(db_main(sys.argv[2:]))

from buscador_midias.cli import main  # noqa: E402

raise SystemExit(main())
