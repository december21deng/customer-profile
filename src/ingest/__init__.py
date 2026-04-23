"""Ingest pipeline for followup records.

Entry point:
    from src.ingest.pipeline import run as run_ingest_pipeline
    await run_ingest_pipeline(record_id)

See docs/ingest-implementation-plan.md for design.
"""

from src.ingest.pipeline import run  # noqa: F401
