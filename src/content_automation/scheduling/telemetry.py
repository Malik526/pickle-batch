"""
telemetry.py — the one structured-log helper for publishing events
(Milestone 3.12, moved out of worker.py in Milestone 4.2 so modules that
worker.py itself imports — scheduling/finalization.py via publish_tiktok —
can log without an import cycle).

log_event(event, **fields) writes one `event=<name> key=value ...` line on
the logger named content_automation.scheduling.worker, the same logger as
before the move, so log output and existing log-capturing tests are
unchanged. Callers pass ids, statuses and codes only — never tokens,
credentials, signed URLs or raw exception text (which can embed platform
response bodies).
"""

import logging

logger = logging.getLogger("content_automation.scheduling.worker")


def log_event(event: str, **fields) -> None:
    logger.info(" ".join([f"event={event}", *(f"{key}={value}" for key, value in fields.items())]))
