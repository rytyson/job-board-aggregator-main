"""
Company discovery and slug validation module.

Two use cases:
  1. Validate existing companies.yaml entries — probe each slug and report
     which ones are returning valid job boards vs. dead (404 / no jobs).

  2. Probe candidate slugs — given a text file of candidate slugs (one per
     line, format: "platform:slug"), test each against the ATS API and print
     which ones respond with valid boards.

Run directly:
    python discovery.py --validate          # test all slugs in companies.yaml
    python discovery.py --probe candidates.txt  # probe new slug candidates
"""

import argparse
import json
import logging
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yaml

from config import COMPANIES_FILE, REQUEST_DELAY_MAX, REQUEST_DELAY_MIN, USER_AGENTS

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def _ua() -> str:
    return random.choice(USER_AGENTS)


def _sleep() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


# ─────────────────────────── Probing functions ──────────────────────────────


def probe_greenhouse(slug: str) -> tuple[bool, int, int]:
    """
    Returns (is_valid, http_status, job_count).
    is_valid = True if the board exists and has ≥1 job posting.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    _sleep()
    try:
        resp = requests.get(url, headers={"User-Agent": _ua()}, timeout=15)
    except Exception:
        return False, 0, 0

    if resp.status_code != 200:
        return False, resp.status_code, 0

    try:
        count = len(resp.json().get("jobs", []))
    except ValueError:
        return False, resp.status_code, 0

    return count > 0, resp.status_code, count


def probe_lever(slug: str) -> tuple[bool, int, int]:
    """Returns (is_valid, http_status, job_count)."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    _sleep()
    try:
        resp = requests.get(url, headers={"User-Agent": _ua()}, timeout=15)
    except Exception:
        return False, 0, 0

    if resp.status_code != 200:
        return False, resp.status_code, 0

    try:
        jobs = resp.json()
        count = len(jobs) if isinstance(jobs, list) else 0
    except ValueError:
        return False, resp.status_code, 0

    return count > 0, resp.status_code, count


def probe_workday(slug: str) -> tuple[bool, int, int]:
    """
    slug format: "company|wd#|site_id"
    Returns (is_valid, http_status, job_count).
    """
    parts = slug.split("|")
    if len(parts) != 3:
        return False, 0, 0

    company, wd_tag, site_id = parts
    wd_num = wd_tag.lstrip("wd")
    base_url = f"https://{company}.wd{wd_num}.myworkdayjobs.com"
    api_url = f"{base_url}/wday/cxs/{company}/{site_id}/jobs"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _ua(),
        "Origin": base_url,
        "Referer": f"{base_url}/{site_id}",
    }
    payload = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}

    _sleep()
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=20)
    except Exception:
        return False, 0, 0

    if resp.status_code != 200:
        return False, resp.status_code, 0

    try:
        total = resp.json().get("total", 0)
    except ValueError:
        return False, resp.status_code, 0

    return total > 0, resp.status_code, total


_PROBERS = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "workday": probe_workday,
}


# ─────────────────────────── Validate existing config ───────────────────────


def validate_existing_companies(max_workers: int = 20) -> None:
    """Probe every slug in companies.yaml and report status."""
    with open(COMPANIES_FILE, "r") as f:
        data = yaml.safe_load(f)

    print(f"\n{'='*60}")
    print("Validating companies.yaml …")
    print(f"{'='*60}\n")

    for platform, prober in _PROBERS.items():
        entries = data.get(platform, []) or []
        if not entries:
            continue

        print(f"── {platform.upper()} ({len(entries)} slugs) ──")
        valid, dead = [], []

        def _check(entry):
            slug = entry["slug"]
            ok, status, count = prober(slug)
            return slug, entry.get("name", slug), ok, status, count

        with ThreadPoolExecutor(max_workers=min(max_workers, len(entries))) as pool:
            futures = {pool.submit(_check, e): e for e in entries}
            for future in as_completed(futures):
                slug, name, ok, status, count = future.result()
                if ok:
                    valid.append((slug, name, count))
                    print(f"  ✓ {slug:<40} ({name}) — {count} jobs")
                else:
                    dead.append((slug, name, status))
                    print(f"  ✗ {slug:<40} ({name}) — HTTP {status}")

        print(f"\n  Valid: {len(valid)}  /  Dead: {len(dead)}\n")
        if dead:
            print("  Dead slugs to review or remove:")
            for slug, name, status in dead:
                print(f"    {slug} [{name}] → HTTP {status}")
        print()


# ─────────────────────────── Probe candidate file ───────────────────────────


def probe_candidates(filepath: str) -> None:
    """
    Read a file of candidate slugs and test each one.

    File format (one entry per line):
        greenhouse:some-slug
        lever:some-slug
        workday:company|wd5|site_id

    Prints which slugs are valid (good candidates to add to companies.yaml).
    """
    with open(filepath, "r") as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    results: list[tuple] = []

    def _probe_line(line: str):
        if ":" not in line:
            return line, "unknown", False, 0, 0
        platform, slug = line.split(":", 1)
        platform = platform.strip().lower()
        slug = slug.strip()
        prober = _PROBERS.get(platform)
        if not prober:
            return line, platform, False, 0, 0
        ok, status, count = prober(slug)
        return slug, platform, ok, status, count

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_probe_line, line): line for line in lines}
        for future in as_completed(futures):
            slug, platform, ok, status, count = future.result()
            results.append((platform, slug, ok, status, count))

    results.sort(key=lambda r: (not r[2], r[0], r[1]))

    print(f"\nProbed {len(lines)} candidates:\n")
    valid_count = 0
    for platform, slug, ok, status, count in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} [{platform}] {slug} — HTTP {status}, {count} jobs")
        if ok:
            valid_count += 1

    print(f"\nValid: {valid_count}/{len(lines)}")
    print("\nAdd valid slugs to companies.yaml:")
    for platform, slug, ok, status, count in results:
        if ok:
            print(f"  - slug: \"{slug}\"  # {platform}, {count} jobs")


# ─────────────────────────── CLI ────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate or discover company slugs for the job alert system"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--validate",
        action="store_true",
        help="Probe all slugs in companies.yaml and report which are active",
    )
    group.add_argument(
        "--probe",
        metavar="FILE",
        help="Probe candidate slugs from FILE (format: 'platform:slug', one per line)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Max parallel workers (default: 20)",
    )
    args = parser.parse_args()

    if args.validate:
        validate_existing_companies(max_workers=args.workers)
    elif args.probe:
        probe_candidates(args.probe)


if __name__ == "__main__":
    main()
