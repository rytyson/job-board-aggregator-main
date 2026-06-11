"""
Platform scrapers for Greenhouse, Lever, and Workday.

Two operating modes:

  chunks (default) — reads the gzipped job chunks already produced by
      scripts/scraper.py.  Covers 21,000+ companies / 1.4 M+ jobs with zero
      extra API calls.  Runs in seconds.

  live — queries companies.yaml directly against each ATS API.  Covers only
      the ~83 curated companies but sees jobs posted since the last daily
      scrape.  Use with --mode live if you need same-day freshness for a
      specific company list.
"""

import gzip
import hashlib
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import yaml

from config import (
    ALLOW_UNSPECIFIED_LOCATION,
    ALLOWED_BARE_LOCATIONS,
    ALLOWED_LOCATIONS,
    CHUNKS_DIR,
    CHUNKS_MANIFEST,
    COMPANIES_FILE,
    EXCLUDED_COUNTRIES,
    EXCLUSION_TERMS,
    MAX_WORKERS_GREENHOUSE,
    MAX_WORKERS_LEVER,
    MAX_WORKERS_WORKDAY,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    TARGET_KEYWORDS,
    USER_AGENTS,
    WORKDAY_MAX_RESULTS_PER_SEARCH,
    WORKDAY_SEARCH_TERMS,
)

log = logging.getLogger(__name__)


# ─────────────────────────── Helpers ────────────────────────────────────────


def _ua() -> str:
    return random.choice(USER_AGENTS)


def _sleep() -> None:
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _make_job_id(platform: str, company_slug: str, raw_id: str) -> str:
    """Return a stable, collision-resistant job ID."""
    key = f"{platform.lower()}:{company_slug}:{raw_id}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_location(raw: str) -> str:
    if not raw:
        return ""
    return raw.strip()[:120]


# ──────────────────────────── Load companies ────────────────────────────────


def load_companies() -> dict:
    """
    Load companies.yaml.

    Returns a dict:
        {
            "greenhouse": [{"slug": ..., "name": ..., "tags": [...]}, ...],
            "lever":      [...],
            "workday":    [...],
        }
    """
    with open(COMPANIES_FILE, "r") as f:
        data = yaml.safe_load(f)

    result = {"greenhouse": [], "lever": [], "workday": []}
    for platform in result:
        entries = data.get(platform) or []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("slug"):
                result[platform].append(entry)

    log.info(
        "Loaded companies: GH=%d  Lever=%d  WD=%d",
        len(result["greenhouse"]),
        len(result["lever"]),
        len(result["workday"]),
    )
    return result


# ──────────────────────────── Greenhouse ────────────────────────────────────


def _fetch_greenhouse(entry: dict) -> list[dict]:
    """Return normalized jobs for one Greenhouse company slug."""
    slug = entry["slug"]
    company_name = entry.get("name", slug)
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    _sleep()
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _ua()},
            timeout=20,
        )
    except Exception as exc:
        log.debug("Greenhouse %s: request error — %s", slug, exc)
        return []

    if resp.status_code == 404:
        log.debug("Greenhouse %s: 404 — slug invalid or board private", slug)
        return []
    if resp.status_code != 200:
        log.debug("Greenhouse %s: HTTP %d", slug, resp.status_code)
        return []

    try:
        jobs = resp.json().get("jobs", [])
    except ValueError:
        return []

    normalized = []
    for job in jobs:
        location = _normalize_location(
            (job.get("location") or {}).get("name", "")
        )
        updated = job.get("updated_at") or ""
        date_posted = updated[:10] if updated else ""
        raw_id = str(job.get("id") or job.get("absolute_url", ""))
        normalized.append(
            {
                "job_id": _make_job_id("greenhouse", slug, raw_id),
                "title": (job.get("title") or "").strip(),
                "company": company_name,
                "company_slug": slug,
                "location": location,
                "application_url": job.get("absolute_url") or "",
                "date_posted": date_posted,
                "platform_source": "Greenhouse",
            }
        )
    return normalized


# ──────────────────────────── Lever ─────────────────────────────────────────


def _fetch_lever(entry: dict) -> list[dict]:
    """Return normalized jobs for one Lever company slug."""
    slug = entry["slug"]
    company_name = entry.get("name", slug)
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"

    _sleep()
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _ua()},
            timeout=20,
        )
    except Exception as exc:
        log.debug("Lever %s: request error — %s", slug, exc)
        return []

    if resp.status_code == 404:
        log.debug("Lever %s: 404", slug)
        return []
    if resp.status_code != 200:
        log.debug("Lever %s: HTTP %d", slug, resp.status_code)
        return []

    try:
        jobs = resp.json()
    except ValueError:
        return []

    if not isinstance(jobs, list):
        return []

    normalized = []
    for job in jobs:
        categories = job.get("categories") or {}
        location = _normalize_location(categories.get("location") or "")
        created_ms = job.get("createdAt") or 0
        date_posted = ""
        if created_ms:
            try:
                date_posted = datetime.fromtimestamp(
                    created_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except Exception:
                pass
        raw_id = job.get("id") or job.get("hostedUrl") or ""
        normalized.append(
            {
                "job_id": _make_job_id("lever", slug, raw_id),
                "title": (job.get("text") or "").strip(),
                "company": company_name,
                "company_slug": slug,
                "location": location,
                "application_url": job.get("hostedUrl") or "",
                "date_posted": date_posted,
                "platform_source": "Lever",
            }
        )
    return normalized


# ──────────────────────────── Workday ───────────────────────────────────────


def _fetch_workday_page(
    api_url: str,
    base_url: str,
    site_id: str,
    search_text: str,
    offset: int,
    limit: int,
    headers: dict,
) -> tuple[list, int]:
    """POST one page of Workday results; return (jobs_list, total)."""
    payload = {
        "appliedFacets": {},
        "limit": limit,
        "offset": offset,
        "searchText": search_text,
    }
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=25)
    except Exception:
        return [], 0

    if resp.status_code != 200:
        return [], 0

    try:
        data = resp.json()
    except ValueError:
        return [], 0

    jobs = data.get("jobPostings", [])
    total = data.get("total", 0)

    normalized = []
    for job in jobs:
        path = job.get("externalPath", "")
        location = _normalize_location(job.get("locationsText") or "")
        normalized.append(
            {
                "_path": path,
                "_base": base_url,
                "_site": site_id,
                "title": (job.get("title") or "").strip(),
                "location": location,
            }
        )
    return normalized, total


def _fetch_workday(entry: dict) -> list[dict]:
    """
    Return normalized jobs for one Workday slug (format: "company|wd#|site_id").

    Uses WORKDAY_SEARCH_TERMS to pre-filter large boards, then deduplicates
    by job path before returning.
    """
    slug = entry["slug"]
    company_name = entry.get("name", slug)

    parts = slug.split("|")
    if len(parts) != 3:
        log.warning("Workday slug has wrong format (expected a|b|c): %s", slug)
        return []

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

    seen_paths: set[str] = set()
    raw_hits: list[dict] = []

    for search_term in WORKDAY_SEARCH_TERMS:
        offset = 0
        limit = 20
        collected = 0

        while collected < WORKDAY_MAX_RESULTS_PER_SEARCH:
            page, total = _fetch_workday_page(
                api_url, base_url, site_id, search_term, offset, limit, headers
            )
            if not page:
                break

            for item in page:
                path = item["_path"]
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    raw_hits.append(item)

            collected += len(page)
            offset += limit

            if offset >= total or offset >= WORKDAY_MAX_RESULTS_PER_SEARCH:
                break

            time.sleep(random.uniform(0.4, 1.0))  # inter-page jitter

        _sleep()

    normalized = []
    for item in raw_hits:
        path = item["_path"]
        url = f"{item['_base']}/{item['_site']}{path}" if path else ""
        raw_id = path or url
        normalized.append(
            {
                "job_id": _make_job_id("workday", slug, raw_id),
                "title": item["title"],
                "company": company_name,
                "company_slug": slug,
                "location": item["location"],
                "application_url": url,
                "date_posted": "",  # Workday CXS doesn't expose posted date
                "platform_source": "Workday",
            }
        )
    return normalized


# ──────────────────────────── Filtering ─────────────────────────────────────


def _location_matches(location: str) -> bool:
    """Return True if the job location is one we want."""
    loc = location.lower().strip()

    if not loc or loc in {"not specified", "n/a", "unknown"}:
        return ALLOW_UNSPECIFIED_LOCATION

    # Reject non-US locations even when they contain "remote".
    # e.g. "China-Remote Location-Beijing" contains "remote" but is not US-remote.
    for country in EXCLUDED_COUNTRIES:
        if country in loc:
            return False

    # Primary patterns (remote, jacksonville, FL, etc.)
    for pattern in ALLOWED_LOCATIONS:
        if pattern.lower() in loc:
            return True

    # Bare country strings — exact match only.
    # "United States" → allow.  "Spring, Texas, United States of America" → deny.
    if loc in ALLOWED_BARE_LOCATIONS:
        return True

    return False


# Pre-compile keyword patterns with word boundaries for precision.
# e.g. "it director" matches "IT Director" but NOT "pursuit director".
_KW_PATTERNS = [
    re.compile(r"\b" + re.escape(kw.strip()) + r"\b", re.IGNORECASE)
    for kw in TARGET_KEYWORDS
]
_EX_PATTERNS = [
    re.compile(r"\b" + re.escape(ex.strip()) + r"\b", re.IGNORECASE)
    for ex in EXCLUSION_TERMS
]


def _title_matches_keywords(title: str) -> bool:
    """Return True if title matches at least one TARGET keyword (word-boundary)."""
    return any(p.search(title) for p in _KW_PATTERNS)


def _title_excluded(title: str) -> bool:
    """Return True if title matches an EXCLUSION term (word-boundary)."""
    return any(p.search(title) for p in _EX_PATTERNS)


def filter_jobs(jobs: list[dict]) -> list[dict]:
    """
    Apply keyword, location, and exclusion filters.

    Also drops jobs without a title or application URL.
    """
    kept = []
    for job in jobs:
        title = job.get("title", "").strip()
        url = job.get("application_url", "").strip()

        if not title or not url:
            continue
        if _title_excluded(title):
            continue
        if not _title_matches_keywords(title):
            continue
        if not _location_matches(job.get("location", "")):
            continue

        kept.append(job)
    return kept


# ──────────────────────────── Chunk-based loader ────────────────────────────


def fetch_all_jobs_from_chunks() -> list[dict]:
    """
    Read all gzipped job chunks produced by scripts/scraper.py and return
    every job that passes the keyword + location + exclusion filters.

    This covers 21,000+ companies / 1.4 M+ jobs with zero API calls.
    It runs in ~15–30 seconds (IO-bound; chunks are local files).

    Chunk format fields (slim subset saved for the frontend):
        title, company, location, url, ats, skill_level,
        is_recruiter, scraped_at, salary
    """
    if not os.path.exists(CHUNKS_MANIFEST):
        raise FileNotFoundError(
            f"Chunk manifest not found at {CHUNKS_MANIFEST}.\n"
            "Run scripts/scraper.py first (or wait for the scrape-jobs Action to complete)."
        )

    with open(CHUNKS_MANIFEST, "r") as f:
        manifest = json.load(f)

    chunk_files = manifest.get("chunks", [])
    total_jobs_in_manifest = manifest.get("totalJobs", 0)
    last_updated = manifest.get("last_updated", "unknown")

    print(f"\n  Reading {len(chunk_files)} chunks  ({total_jobs_in_manifest:,} jobs, updated {last_updated[:10]})")

    all_raw: list[dict] = []

    for i, chunk_filename in enumerate(chunk_files):
        chunk_path = os.path.join(CHUNKS_DIR, chunk_filename)
        if not os.path.exists(chunk_path):
            log.warning("Chunk file missing: %s — skipping", chunk_path)
            continue
        with gzip.open(chunk_path, "rt", encoding="utf-8") as f:
            jobs = json.load(f)
        all_raw.extend(jobs)
        if (i + 1) % 10 == 0 or (i + 1) == len(chunk_files):
            print(f"  Loaded {i+1}/{len(chunk_files)} chunks — {len(all_raw):,} jobs so far")

    # Normalize chunk format → alert system format, then filter
    normalized: list[dict] = []
    for job in all_raw:
        url = (job.get("url") or "").strip()
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        ats = job.get("ats") or "Unknown"
        location = _normalize_location(job.get("location") or "")
        scraped_at = job.get("scraped_at") or ""
        date_posted = scraped_at[:10] if scraped_at else ""

        if not url or not title or not company:
            continue

        # iCIMS scraper does not populate location — infer from the remote flag.
        # iCIMS is used almost exclusively by US enterprises, so "United States"
        # is a safe default when remote=False and location is absent.
        # Exception: skip iCIMS boards whose company slug starts with a
        # 2-letter country code prefix (e.g. "de-merlin", "it-merlin", "uk-company")
        # — those are country-specific non-US boards.
        _ICIMS_COUNTRY_PREFIXES = (
            "de-", "it-", "uk-", "fr-", "es-", "nl-", "au-", "ca-",
            "sg-", "jp-", "kr-", "in-", "br-", "mx-", "pl-", "se-",
        )
        loc_raw = location.lower().strip()
        if ats == "iCIMS" and loc_raw in ("", "not specified", "n/a", "unknown"):
            slug_lower = company.lower().replace(" ", "-")
            if any(slug_lower.startswith(pfx) for pfx in _ICIMS_COUNTRY_PREFIXES):
                location = ""   # will fail location filter → excluded
            else:
                location = "Remote" if job.get("remote") else "United States"

        # Generate a stable job_id from the three most-stable fields
        job_id = _make_job_id(ats, company, url)

        normalized.append(
            {
                "job_id": job_id,
                "title": title,
                "company": company,
                "company_slug": company.lower().replace(" ", "-"),
                "location": location,
                "application_url": url,
                "date_posted": date_posted,
                "platform_source": ats,
                # pass-through extras (useful for display)
                "skill_level": job.get("skill_level", ""),
                "is_recruiter": job.get("is_recruiter", False),
            }
        )

    print(f"\n  {len(normalized):,} jobs normalized → filtering …")
    filtered = filter_jobs(normalized)
    print(f"  {len(filtered)} jobs matched filters")
    return filtered


# ──────────────────────────── Main fetch orchestration ───────────────────────


def _run_platform(
    entries: list[dict],
    fetcher,
    platform_name: str,
    max_workers: int,
) -> list[dict]:
    """Fetch all companies on one platform in parallel, then filter."""
    print(f"\n{'─'*60}")
    print(f"  {platform_name}: querying {len(entries)} companies …")

    all_raw: list[dict] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetcher, e): e for e in entries}
        for future in as_completed(futures):
            completed += 1
            try:
                jobs = future.result()
                if jobs:
                    all_raw.extend(jobs)
                    entry = futures[future]
                    log.debug(
                        "  %s / %s: %d jobs",
                        platform_name,
                        entry["slug"],
                        len(jobs),
                    )
            except Exception as exc:
                log.debug("Worker error: %s", exc)

            if completed % 10 == 0 or completed == len(entries):
                print(f"  {platform_name}: {completed}/{len(entries)} done, {len(all_raw)} raw jobs")

    filtered = filter_jobs(all_raw)
    print(f"  {platform_name}: {len(all_raw)} raw → {len(filtered)} after filtering")
    return filtered


def fetch_all_jobs(companies: dict) -> list[dict]:
    """
    Fetch and filter jobs from all three platforms.

    Returns a flat list of normalized, filtered job dicts.
    """
    results: list[dict] = []

    results += _run_platform(
        companies.get("greenhouse", []),
        _fetch_greenhouse,
        "Greenhouse",
        MAX_WORKERS_GREENHOUSE,
    )
    results += _run_platform(
        companies.get("lever", []),
        _fetch_lever,
        "Lever",
        MAX_WORKERS_LEVER,
    )
    results += _run_platform(
        companies.get("workday", []),
        _fetch_workday,
        "Workday",
        MAX_WORKERS_WORKDAY,
    )

    # Final dedup by job_id (in case a slug appears in multiple platforms)
    seen: set[str] = set()
    unique: list[dict] = []
    for job in results:
        jid = job["job_id"]
        if jid not in seen:
            seen.add(jid)
            unique.append(job)

    return unique
