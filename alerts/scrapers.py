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
        # Don't trust date_posted from chunks — it reflects when the chunk scraper
        # ran, not when the job was actually posted. We extract the real date from
        # the posting page during the liveness check and populate it there.
        date_posted = None

        if not url or not title or not company:
            continue

        # iCIMS location handling — chunks store location in two formats:
        #   1. Empty / "Not specified"  → infer from remote flag
        #   2. Structured code like "US-CA-Valencia" or "LB-Beirut" → parse
        # Only pass Remote jobs and FL (Jacksonville) jobs; exclude everything else.
        _ICIMS_INTL_PREFIXES = (
            "de-", "it-", "uk-", "fr-", "es-", "nl-", "au-", "ca-",
            "sg-", "jp-", "kr-", "in-", "br-", "mx-", "pl-", "se-",
        )
        if ats == "iCIMS":
            loc_raw = location.strip()
            loc_lower = loc_raw.lower()

            if loc_lower in ("", "not specified", "n/a", "unknown"):
                # No location data in chunk — use company slug as last resort to
                # catch known international iCIMS boards.
                slug_lower = company.lower().replace(" ", "-")
                if any(slug_lower.startswith(pfx) for pfx in _ICIMS_INTL_PREFIXES):
                    location = ""   # known non-US board → exclude
                elif job.get("remote"):
                    location = "Remote"
                else:
                    # Non-remote, no location in chunk. iCIMS is used almost
                    # exclusively by US enterprises, so "United States" is a
                    # reasonable default. The liveness check will verify the real
                    # location from the posting page and exclude international hits.
                    location = "United States"

            elif re.match(r"^[A-Za-z]{2}-", loc_raw):
                # Structured country-code prefix: "US-CA-Valencia", "LB-Beirut", etc.
                country_code = loc_raw[:2].upper()
                if country_code == "US":
                    if job.get("remote"):
                        location = "Remote"
                    else:
                        parts = loc_raw.split("-")
                        state = parts[1].upper() if len(parts) > 1 else ""
                        city  = parts[2] if len(parts) > 2 else ""
                        if state == "FL":
                            location = f"{city}, FL" if city else "Florida"
                        else:
                            location = ""   # US onsite outside FL → exclude
                else:
                    location = ""   # International → exclude

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


# ──────────────────────────── Liveness check + salary ───────────────────────

_CLOSED_PHRASES = [
    # Generic ATS closure messages
    "this job is no longer available",
    "job has been closed",
    "position has been filled",
    "no longer accepting applications",
    "requisition is closed",
    "job has been removed",
    "position is no longer available",
    "this opening has been filled",
    "this position has been filled",
    "job has expired",
    "posting has expired",
    "this job has expired",
    "job is no longer active",
    "no longer active",
    "position is closed",
    "not accepting applications",
    "job posting has been removed",
    "sorry, this job is no longer",
    "this opportunity is no longer",
    "job listing has been removed",
    "opening is no longer available",
    # Workday-specific (Workday returns HTTP 200 with these error pages)
    "page you are looking for doesn't exist",
    "page you are looking for does not exist",
    "the page you requested was not found",
    "this page no longer exists",
    "oops! the page",
    "we couldn't find this page",
    "this job requisition is no longer open",
    "this requisition is no longer open",
    # Generic 404-style messages returned as HTTP 200
    "404 - page not found",
    "error 404",
    "page cannot be found",
]

# Regexes used for real posted-date extraction from page HTML
_WD_POSTED_DATE_RE  = re.compile(r'"postedOn"\s*:\s*"([^"]+)"', re.IGNORECASE)
_TIME_DATETIME_RE   = re.compile(r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2})', re.IGNORECASE)
_DAYS_AGO_RE        = re.compile(r'(\d+)\s+days?\s+ago', re.IGNORECASE)
_POSTED_LABEL_RE    = re.compile(
    r'posted(?:\s+on)?\s*[:\-]?\s*'
    r'(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}'
    r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w{0,6}\.?\s+\d{1,2},?\s+\d{4})',
    re.IGNORECASE,
)

# Regex to detect iCIMS structured location codes in page HTML
_ICIMS_PAGE_LOC_RE  = re.compile(r'\b([A-Z]{2})-([A-Z]{2})-\w+\b|\b([A-Z]{2})-[A-Za-z]+\b', re.IGNORECASE)
# Country codes that are never US states (used to distinguish "LB-Beirut" from "US-CA-*")
_KNOWN_COUNTRY_CODES = {
    "AF","AL","DZ","AD","AO","AG","AR","AM","AU","AT","AZ","BS","BH","BD","BB",
    "BY","BE","BZ","BJ","BT","BO","BA","BW","BR","BN","BG","BF","BI","CV","KH",
    "CM","CF","TD","CL","CN","CO","KM","CD","CG","CR","HR","CU","CY","CZ","DK",
    "DJ","DM","DO","EC","EG","SV","GQ","ER","EE","SZ","ET","FJ","FI","FR","GA",
    "GM","GE","DE","GH","GR","GD","GT","GN","GW","GY","HT","HN","HU","IS","IN",
    "ID","IR","IQ","IE","IL","IT","JM","JP","JO","KZ","KE","KI","KP","KR","KW",
    "KG","LA","LV","LB","LS","LR","LY","LI","LT","LU","MG","MW","MY","MV","ML",
    "MT","MH","MR","MU","MX","FM","MD","MC","MN","ME","MA","MZ","MM","NA","NR",
    "NP","NL","NZ","NI","NE","NG","MK","NO","OM","PK","PW","PA","PG","PY","PE",
    "PH","PL","PT","QA","RO","RU","RW","KN","LC","VC","WS","SM","ST","SA","SN",
    "RS","SC","SL","SG","SK","SI","SB","SO","ZA","SS","ES","LK","SD","SR","SE",
    "CH","SY","TW","TJ","TZ","TH","TL","TG","TO","TT","TN","TR","TM","TV","UG",
    "UA","AE","GB","UK","UY","UZ","VU","VE","VN","YE","ZM","ZW",
}


def _normalise_date(raw: str) -> str | None:
    """Parse various date formats to YYYY-MM-DD, or return None if unrecognised."""
    from datetime import datetime
    raw = raw.strip().rstrip(".")
    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
        return raw
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', raw)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    for fmt in ('%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y',
                '%d %B %Y', '%d %b %Y', '%B %d,%Y'):
        try:
            return datetime.strptime(raw, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return None


def _extract_post_date(html: str) -> str | None:
    """
    Try to extract the real posting date from job page HTML.
    Returns YYYY-MM-DD string or None.
    """
    from datetime import date, timedelta

    # Workday embeds postedOn in page JSON: "postedOn":"06/04/2026"
    m = _WD_POSTED_DATE_RE.search(html)
    if m:
        d = _normalise_date(m.group(1))
        if d:
            return d

    # Greenhouse / Lever / Ashby use <time datetime="2026-06-04">
    m = _TIME_DATETIME_RE.search(html)
    if m:
        return m.group(1)

    # "Posted 7 days ago" → calculate from today
    m = _DAYS_AGO_RE.search(html)
    if m:
        days = int(m.group(1))
        if days <= 365:  # sanity cap
            return (date.today() - timedelta(days=days)).isoformat()

    # "Posted on June 4, 2026" / "Date Posted: 06/04/2026"
    m = _POSTED_LABEL_RE.search(html)
    if m:
        return _normalise_date(m.group(1))

    return None


def _icims_location_from_page(html: str) -> str | None:
    """
    For iCIMS jobs where the chunk had no location, read the real location
    from the posting page HTML.

    Returns:
      "Remote"       → remote job, keep
      "Florida"      → onsite FL (incl. Jacksonville), keep
      "US-ONSITE"    → US onsite but outside FL, exclude
      "INTL:{CC}"    → international, exclude
      None           → couldn't determine, pass through unchanged
    """
    for m in _ICIMS_PAGE_LOC_RE.finditer(html):
        # Three-part code: CC-ST-City  e.g. US-TN-Memphis, US-FL-Jacksonville
        if m.group(1) and m.group(2):
            country = m.group(1).upper()
            state   = m.group(2).upper()
            if country == "US":
                return "Florida" if state == "FL" else "US-ONSITE"
            if country in _KNOWN_COUNTRY_CODES:
                return f"INTL:{country}"
        # Two-part code: CC-City  e.g. LB-Beirut
        elif m.group(3):
            country = m.group(3).upper()
            if country == "US":
                return None   # ambiguous — don't change anything
            if country in _KNOWN_COUNTRY_CODES:
                return f"INTL:{country}"
    # Explicit Remote indicator on iCIMS pages
    if re.search(r'remote[^<\n]{0,30}(?:yes|true|only)|\bwork from home\b', html.lower()):
        return "Remote"
    return None


# Regex to parse Workday job URLs and extract company/tenant/job-id
_WD_URL_RE = re.compile(
    r'https://([^.]+)(\.wd\d+\.myworkdayjobs\.com)/([^/]+)/job/.+/[^_/]+_([A-Za-z0-9\-]+?)(?:/|\?|$)',
    re.IGNORECASE,
)

_SALARY_LABELS = [
    "salary", "compensation", "pay range", "base pay",
    "annual salary", "total compensation", "wage", "hourly rate",
    "base salary", "salary range",
]

_SALARY_RE = re.compile(
    r'\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*[kK]?\s*(?:[-–—]\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)\s*[kK]?)?',
    re.IGNORECASE,
)


def _parse_salary_val(s: str) -> int:
    s = s.replace(",", "").strip()
    val = float(s)
    if val < 1000:
        val *= 1000
    return int(val)


def _extract_salary(html: str) -> str | None:
    text_lower = html.lower()
    for label in _SALARY_LABELS:
        pos = text_lower.find(label)
        if pos == -1:
            continue
        chunk = html[max(0, pos - 20) : pos + 400]
        for m in _SALARY_RE.finditer(chunk):
            low_str = m.group(1)
            high_str = m.group(2)
            try:
                low = _parse_salary_val(low_str)
                if low < 25_000 or low > 2_000_000:
                    continue
                if high_str:
                    high = _parse_salary_val(high_str)
                    if high < low:
                        high, low = low, high
                    if high > 2_000_000:
                        continue
                    return f"${low:,} – ${high:,}"
                return f"${low:,}"
            except (ValueError, TypeError):
                continue
    return None


def _check_one(job: dict) -> dict:
    url      = (job.get("application_url") or "").strip()
    platform = job.get("platform_source", "")
    checked_at = _now_iso()

    def _closed():
        return {**job, "is_live": False, "salary_posted": None,
                "date_posted": None, "liveness_checked_at": checked_at}

    def _live_from_html(html: str):
        return {**job, "is_live": True,
                "salary_posted": _extract_salary(html),
                "date_posted": _extract_post_date(html),
                "liveness_checked_at": checked_at}

    if not url:
        return _closed()

    # ── Workday: use CXS JSON API — Workday is a JS SPA so HTML gives nothing ──
    wd_m = _WD_URL_RE.match(url)
    if wd_m:
        company = wd_m.group(1)
        wd_host = company + wd_m.group(2)
        tenant  = wd_m.group(3)
        job_id  = wd_m.group(4)
        try:
            api_url = f"https://{wd_host}/wday/cxs/{company}/{tenant}/jobs"
            r = requests.post(
                api_url,
                json={"limit": 1, "offset": 0, "searchText": job_id},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": _ua(),
                    "Accept": "application/json",
                },
                timeout=12,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("total", 0) == 0:
                    return _closed()   # job not found in Workday → closed/removed
                # Extract posted date from postedOn field ("Posted 7 Days Ago" etc.)
                postings = data.get("jobPostings", [])
                date_p = None
                if postings:
                    posted_on = postings[0].get("postedOn") or ""
                    date_p = _extract_post_date(posted_on)
                return {**job, "is_live": True, "salary_posted": None,
                        "date_posted": date_p, "liveness_checked_at": checked_at}
        except Exception:
            pass   # fall through to HTML approach if API fails

    # ── All other platforms: fetch HTML and inspect ────────────────────────
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _ua(), "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=12,
            allow_redirects=True,
        )

        if resp.status_code in (404, 410):
            return _closed()
        if resp.status_code >= 400:
            return _closed()

        # Greenhouse (and some others) redirect to ?error=true when job is removed
        if "error=true" in resp.url.lower():
            return _closed()

        # Detect redirect to a careers home page (job depth drops significantly)
        def _path_depth(u: str) -> int:
            return len([p for p in u.split("/") if p and "http" not in p])

        orig_d  = _path_depth(url)
        final_d = _path_depth(resp.url)
        if (orig_d - final_d) >= 2 and final_d <= 2:
            return _closed()

        content_lower = resp.text.lower()
        for phrase in _CLOSED_PHRASES:
            if phrase in content_lower:
                return _closed()

        # iCIMS jobs defaulted to "United States": verify real location from page.
        # Exclude if the page confirms US onsite non-FL or international.
        if platform == "iCIMS" and job.get("location") == "United States":
            real_loc = _icims_location_from_page(resp.text)
            if real_loc in ("US-ONSITE",) or (real_loc and real_loc.startswith("INTL:")):
                return _closed()

        return _live_from_html(resp.text)

    except requests.exceptions.Timeout:
        return {**job, "is_live": True, "salary_posted": None,
                "date_posted": None, "liveness_checked_at": checked_at}
    except Exception:
        return {**job, "is_live": True, "salary_posted": None,
                "date_posted": None, "liveness_checked_at": checked_at}


def check_jobs_liveness(jobs: list[dict], max_workers: int = 15) -> list[dict]:
    """Check liveness + salary for all jobs in parallel. Returns enriched list."""
    results: list[dict] = []
    total = len(jobs)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_check_one, job): job for job in jobs}
        for future in as_completed(futures):
            results.append(future.result())
            done = len(results)
            if done % 20 == 0 or done == total:
                live = sum(1 for r in results if r.get("is_live", True))
                print(f"  Liveness: {done}/{total} checked — {live} live so far …")

    return results
