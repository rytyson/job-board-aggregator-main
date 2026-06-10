"""
Deduplication and pruning for Ryan's job alert system.

seen_jobs.json schema:
    {
        "<job_id>": {
            "first_seen": "YYYY-MM-DD",
            "last_seen":  "YYYY-MM-DD"
        },
        ...
    }

A job is "new" when its job_id is NOT yet in seen_jobs.json.
A job is "stale" when it hasn't appeared in any scrape for PRUNE_AFTER_DAYS days.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta

from config import PRUNE_AFTER_DAYS, SEEN_JOBS_FILE

log = logging.getLogger(__name__)


def load_seen_jobs() -> dict:
    """Load seen_jobs.json; return an empty dict if the file doesn't exist yet."""
    if not os.path.exists(SEEN_JOBS_FILE):
        log.info("seen_jobs.json not found — starting fresh")
        return {}
    try:
        with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("seen_jobs.json has unexpected format; resetting")
            return {}
        log.info("Loaded %d seen job IDs", len(data))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not load seen_jobs.json: %s — starting fresh", exc)
        return {}


def save_seen_jobs(seen: dict) -> None:
    """Write seen_jobs.json atomically (write temp, rename)."""
    tmp = SEEN_JOBS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, sort_keys=True)
    os.replace(tmp, SEEN_JOBS_FILE)
    log.info("Saved %d entries to seen_jobs.json", len(seen))


def partition_jobs(
    jobs: list[dict], seen: dict
) -> tuple[list[dict], list[dict], dict]:
    """
    Split jobs into new vs. previously-seen, and update seen dict.

    Returns:
        new_jobs      — jobs whose job_id was NOT in seen before this run
        existing_jobs — jobs whose job_id WAS already in seen
        updated_seen  — seen dict with last_seen refreshed for all current jobs
    """
    today = date.today().isoformat()
    new_jobs: list[dict] = []
    existing_jobs: list[dict] = []

    for job in jobs:
        jid = job["job_id"]
        if jid in seen:
            seen[jid]["last_seen"] = today
            existing_jobs.append(job)
        else:
            seen[jid] = {"first_seen": today, "last_seen": today}
            new_jobs.append(job)

    return new_jobs, existing_jobs, seen


def prune_seen_jobs(seen: dict, current_job_ids: set[str]) -> dict:
    """
    Remove stale entries from seen dict.

    An entry is stale if:
      - Its job_id is NOT in current_job_ids (job disappeared from all boards)
      - AND it was last seen more than PRUNE_AFTER_DAYS days ago.

    Jobs still open (present in current_job_ids) are never pruned.
    """
    cutoff = date.today() - timedelta(days=PRUNE_AFTER_DAYS)
    pruned: list[str] = []

    for jid, meta in list(seen.items()):
        if jid in current_job_ids:
            continue  # still live — keep unconditionally

        last_seen_str = meta.get("last_seen", "")
        try:
            last_seen = date.fromisoformat(last_seen_str)
        except ValueError:
            last_seen = date.min  # malformed date → prune immediately

        if last_seen < cutoff:
            pruned.append(jid)
            del seen[jid]

    if pruned:
        log.info("Pruned %d stale job IDs from seen_jobs.json", len(pruned))

    return seen


def enrich_jobs_with_seen_meta(jobs: list[dict], seen: dict) -> list[dict]:
    """
    Attach first_seen / last_seen metadata from seen dict to each job dict.

    Call this before saving jobs_db.json so the HTML page can sort by date.
    """
    for job in jobs:
        meta = seen.get(job["job_id"], {})
        job["first_seen"] = meta.get("first_seen", "")
        job["last_seen"] = meta.get("last_seen", "")
    return jobs
