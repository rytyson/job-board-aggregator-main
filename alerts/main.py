"""
Main entry point for Ryan's IT-leadership job alert system.

Usage:
    python main.py                   # chunks mode (default) — filter 1.4M jobs from 21k+ companies
    python main.py --mode live       # live mode — scrape 83 companies in companies.yaml directly
    python main.py --dry-run         # fetch/filter but don't write output files
    python main.py --discover        # validate companies.yaml slugs then exit
    python main.py --reset-seen      # clear seen_jobs.json (re-surfaces all open jobs as new)

Modes:
    chunks (default)
        Reads data/chunks/jobs_chunk_*.json.gz — pre-built by scripts/scraper.py
        (daily via scrape-jobs.yml).  Covers 21,000+ companies / 1.4M+ jobs.
        Runs in ~30 seconds with zero API calls.  Best for daily use.

    live
        Queries companies.yaml directly against each ATS API.
        Covers ~83 curated companies only, but sees same-day postings.
        Use when you've just added new companies and want instant results.
"""

import argparse
import logging
import os
import sys

# Ensure sibling modules are importable when run from any working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import JOBS_DB_FILE, JOB_FEED_FILE, SEEN_JOBS_FILE
from dedup import (
    enrich_jobs_with_seen_meta,
    load_seen_jobs,
    partition_jobs,
    prune_seen_jobs,
    save_seen_jobs,
)
from discovery import validate_existing_companies
from feed_generator import generate_rss_feed, save_jobs_db
from scrapers import check_jobs_liveness, fetch_all_jobs, fetch_all_jobs_from_chunks, load_companies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _banner(text: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def run(dry_run: bool = False, mode: str = "chunks") -> int:
    """
    Execute one full alert cycle.

    mode: "chunks" (default) or "live"
    Returns the number of new jobs found (useful for CI step summaries).
    """
    _banner(f"Ryan's IT-Leadership Job Alert System  [{mode.upper()} mode]")

    # ── 1. Load / scrape jobs ─────────────────────────────────────────────
    if mode == "chunks":
        print("\n[1/6] Reading pre-built job chunks (21,000+ companies) …")
        matching_jobs = fetch_all_jobs_from_chunks()
    else:
        print("\n[1/6] Loading company list for live scrape …")
        companies = load_companies()
        total_companies = sum(len(v) for v in companies.values())
        print(f"      {total_companies} companies across 3 platforms")
        print("\n[2/6] Fetching and filtering jobs …")
        matching_jobs = fetch_all_jobs(companies)

    print(f"\n      {len(matching_jobs)} jobs matched keyword/location filters")

    if not matching_jobs:
        print("      No matching jobs found. Check filters in config.py.")

    # ── 2. Liveness check + salary extraction ────────────────────────────
    print(f"\n[2/6] Checking liveness & salary for {len(matching_jobs)} job(s) …")
    print("      (fetches each posting URL in parallel — takes ~30–90 seconds)")
    matching_jobs = check_jobs_liveness(matching_jobs)
    live_jobs = [j for j in matching_jobs if j.get("is_live", True)]
    closed_count = len(matching_jobs) - len(live_jobs)
    salary_count = sum(1 for j in live_jobs if j.get("salary_posted"))
    print(f"      ✓ {len(live_jobs)} live  |  {closed_count} closed/expired (excluded)")
    print(f"      ✓ {salary_count} postings with salary data")
    matching_jobs = live_jobs  # drop closed jobs before dedup

    # ── 3. Dedup ──────────────────────────────────────────────────────────
    print("\n[3/6] Running deduplication …")
    seen = load_seen_jobs()
    new_jobs, existing_jobs, seen = partition_jobs(matching_jobs, seen)
    print(f"      New: {len(new_jobs)}  |  Previously seen (still open): {len(existing_jobs)}")

    # ── 4. Prune stale entries ────────────────────────────────────────────
    print("\n[4/6] Pruning stale entries …")
    current_ids = {j["job_id"] for j in matching_jobs}
    seen = prune_seen_jobs(seen, current_ids)

    # ── 5. Write outputs ──────────────────────────────────────────────────
    print("\n[5/6] Writing output files …")

    if dry_run:
        print("      DRY-RUN mode — no files written")
        _print_summary(new_jobs, matching_jobs)
        return len(new_jobs)

    all_jobs_with_meta = enrich_jobs_with_seen_meta(matching_jobs, seen)
    save_jobs_db(all_jobs_with_meta)
    generate_rss_feed(new_jobs)
    save_seen_jobs(seen)

    # ── 6. Summary ────────────────────────────────────────────────────────
    print("\n[6/6] Done.")
    _print_summary(new_jobs, matching_jobs)

    print(f"\n  Output files:")
    print(f"    {JOBS_DB_FILE}")
    print(f"    {JOB_FEED_FILE}")
    print(f"    {SEEN_JOBS_FILE}")

    return len(new_jobs)


def _print_summary(new_jobs: list[dict], all_jobs: list[dict]) -> None:
    _banner("Summary")

    by_platform: dict[str, int] = {}
    for j in all_jobs:
        p = j.get("platform_source", "Unknown")
        by_platform[p] = by_platform.get(p, 0) + 1

    print(f"  Total matching open jobs: {len(all_jobs)}")
    for platform, count in sorted(by_platform.items()):
        print(f"    {platform:<15} {count}")

    print(f"\n  NEW this run: {len(new_jobs)}")
    if new_jobs:
        for job in sorted(new_jobs, key=lambda j: j.get("company", ""))[:20]:
            loc = job.get("location") or "—"
            print(f"    [{job['platform_source']:<11}] {job['company']:<30} {job['title'][:55]}")
            print(f"                        📍 {loc}")
        if len(new_jobs) > 20:
            print(f"    … and {len(new_jobs) - 20} more")


def reset_seen() -> None:
    """Delete seen_jobs.json so all currently-open jobs appear as new on next run."""
    if os.path.exists(SEEN_JOBS_FILE):
        os.remove(SEEN_JOBS_FILE)
        print(f"Deleted {SEEN_JOBS_FILE}")
        print("All open jobs will surface as new on the next run.")
    else:
        print("seen_jobs.json does not exist — nothing to reset.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ryan's IT-leadership job alert system"
    )
    parser.add_argument(
        "--mode",
        choices=["chunks", "live"],
        default="chunks",
        help=(
            "chunks (default): filter 1.4M jobs from pre-built data/chunks/ — covers 21k+ companies, "
            "zero API calls, ~30 sec.  "
            "live: scrape companies.yaml directly — 83 companies, same-day freshness."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and filter but don't write any output files",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Run slug validation against companies.yaml, then exit",
    )
    parser.add_argument(
        "--reset-seen",
        action="store_true",
        help="Delete seen_jobs.json so all open jobs surface as new on the next run",
    )
    args = parser.parse_args()

    if args.reset_seen:
        reset_seen()
        return

    if args.discover:
        validate_existing_companies()
        return

    new_count = run(dry_run=args.dry_run, mode=args.mode)
    print(f"\nDone — {new_count} new job(s) written to RSS feed.\n")


if __name__ == "__main__":
    main()
