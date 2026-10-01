"""`python -m surakshasetu_ingest ingest|verify|chunks|bakeoff-embed` (make kb-ingest, kb-verify,
kb-chunks, bakeoff-embed).

chunks lists the approved chunk ids and breadcrumbs without indexing anything, so golden sets can be
relabelled deliberately after a content change. bakeoff-embed is Step 12's embedder comparison.
"""

import argparse
import logging

from dagster import materialize

from surakshasetu.logging import configure_logging
from surakshasetu_ingest import assets
from surakshasetu_ingest.bakeoff import bakeoff_embed
from surakshasetu_ingest.verify import verify

logger = logging.getLogger("surakshasetu.kb.main")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m surakshasetu_ingest")
    parser.add_argument("command", choices=["ingest", "verify", "chunks", "bakeoff-embed"])
    command = parser.parse_args().command
    settings = assets.IngestSettings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    if command == "bakeoff-embed":
        return bakeoff_embed(settings)
    kb = assets.Kb()
    if command == "verify":
        return 1 if verify(kb, settings) else 0
    if command == "ingest":
        materialize(assets.ASSETS, resources={"kb": kb})
        return 0
    result = materialize(assets.ASSETS[:5], resources={"kb": kb})  # through the review gate
    for payload in result.output_for_node("approved").items:
        logger.info("%s  %s", payload.chunk_id, " › ".join(payload.section_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
