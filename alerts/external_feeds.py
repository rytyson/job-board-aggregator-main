"""
External job board fetchers: Himalayas, Remotive, We Work Remotely, Jooble,
JSearch (Google Jobs via RapidAPI), JobsPipe (100+ boards via RapidAPI).

Each fetcher normalises results to the same schema used by the ATS pipeline
and applies the same keyword + location filters so only relevant roles appear.

Guaranteed keys on every returned job dict:
    job_id          str  — SHA1[:16] of "ext:{source}:{url}"
    title           str
    company         str
    location        str
    application_url str
    date_posted     str  — ISO-8601 date ("YYYY-MM-DD") or ""
    salary          str  — raw salary string or ""
    platform_source str  — "Himalayas" | "Remotive" | "We Work Remotely" |
                           "Jooble" | "JSearch (Google Jobs)" | "JobsPipe"
    is_verified     bool — always False; shown as "Unverified" in the UI
    first_seen      str  — ISO-8601 datetime set at fetch time
"""

import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from scrapers import filter_jobs

log = logging.getLogger(__name__)

_JOOBLE_KEY_ENV = "JOOBLE_API_KEY"
_RAPIDAPI_KEY_ENV = "RAPIDAPI_KEY"
_CUTOFF_DAYS = 7
_TIMEOUT = 12  # seconds per request

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ─────────────────────────── helpers ────────────────────────────────────────


def _job_id(source: str, url: str) -> str:
    return hashlib.sha1(f"ext:{source}:{url}".encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff() -> datetime:
    return _now() - timedelta(days=_CUTOFF_DAYS)


def _iso_date(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d")


def _first_seen() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _is_recent(dt: datetime | None) -> bool:
    """Return True if dt is within the 7-day cutoff (or if date unknown)."""
    if dt is None:
        return True  # keep if we couldn't parse the date
    # Ensure dt is timezone-aware for comparison
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= _cutoff()


def _parse_iso(raw: str) -> datetime | None:
    """Parse ISO-8601 string (with or without fractional seconds) → datetime."""
    if not raw:
        return None
    # Trim fractional seconds beyond microseconds (Jooble sends 7 decimal places)
    raw = re.sub(r'(\.\d{6})\d+', r'\1', raw)
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_rss_date(raw: str) -> datetime | None:
    """Parse RFC 822 date from RSS <pubDate>."""
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        return None


def _salary_str(min_sal, max_sal, period=None) -> str:
    if not min_sal and not max_sal:
        return ""
    parts = []
    if min_sal:
        parts.append(f"${min_sal:,}")
    if max_sal:
        parts.append(f"${max_sal:,}")
    base = "–".join(parts)
    if period:
        base += f"/{period}"
    return base


def _make_job(
    source: str,
    title: str,
    company: str,
    location: str,
    url: str,
    date_posted: str,
    salary: str,
) -> dict:
    fs = _first_seen()
    return {
        "job_id": _job_id(source, url),
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "application_url": url.strip(),
        "date_posted": date_posted,
        "first_seen": fs,
        "salary_posted": salary.strip() if salary else "",
        "platform_source": source,
        "is_verified": False,
    }


# ─────────────────────────── Himalayas ──────────────────────────────────────

_HIMALAYAS_QUERIES = [
    "IT Director",
    "IT Manager",
    "Director of IT",
    "Director of Infrastructure",
    "Head of IT",
    "VP of IT",
    "Director of Technology",
]


def fetch_himalayas() -> list[dict]:
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for query in _HIMALAYAS_QUERIES:
        try:
            resp = requests.get(
                "https://himalayas.app/jobs/api/search",
                params={"q": query, "limit": 50},
                headers={"User-Agent": _UA, "Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Himalayas query %r failed: %s", query, exc)
            continue

        for item in data.get("jobs", []):
            url = (item.get("applicationLink") or "").strip()
            if not url or url in seen_urls:
                continue

            pub = item.get("pubDate")
            dt = datetime.fromtimestamp(pub, tz=timezone.utc) if pub else None
            if not _is_recent(dt):
                continue

            loc_list = item.get("locationRestrictions") or []
            location = ", ".join(loc_list) if loc_list else "Remote"

            salary = _salary_str(
                item.get("minSalary"),
                item.get("maxSalary"),
                item.get("salaryPeriod"),
            )

            seen_urls.add(url)
            jobs.append(_make_job(
                source="Himalayas",
                title=item.get("title", ""),
                company=item.get("companyName", ""),
                location=location,
                url=url,
                date_posted=_iso_date(dt),
                salary=salary,
            ))

    filtered = filter_jobs(jobs)
    log.info("Himalayas: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── Remotive ───────────────────────────────────────


def fetch_remotive() -> list[dict]:
    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"category": "information-technology", "limit": 100},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("Remotive fetch failed: %s", exc)
        return []

    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for item in data.get("jobs", []):
        url = (item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue

        dt = _parse_iso(item.get("publication_date", ""))
        if not _is_recent(dt):
            continue

        seen_urls.add(url)
        jobs.append(_make_job(
            source="Remotive",
            title=item.get("title", ""),
            company=item.get("company_name", ""),
            location=item.get("candidate_required_location", "Remote") or "Remote",
            url=url,
            date_posted=_iso_date(dt),
            salary=item.get("salary", "") or "",
        ))

    filtered = filter_jobs(jobs)
    log.info("Remotive: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── We Work Remotely ───────────────────────────────

_WWR_TITLE_RE = re.compile(r'^(.+?):\s+(.+)$')


def fetch_wwr() -> list[dict]:
    try:
        resp = requests.get(
            "https://weworkremotely.com/remote-jobs.rss",
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        log.warning("We Work Remotely fetch failed: %s", exc)
        return []

    jobs: list[dict] = []
    seen_urls: set[str] = set()
    ns = {"wwr": "https://weworkremotely.com/"}  # namespace (may not be present)

    channel = root.find("channel")
    if channel is None:
        return []

    for item in channel.findall("item"):
        # Some items are category headers without a real link — skip them.
        link_el = item.find("link")
        # RSS <link> is sometimes CDATA or a text node after the element
        url = ""
        if link_el is not None and link_el.text:
            url = link_el.text.strip()
        # Fallback: check CDATA-wrapped <link>
        if not url:
            # Try to get text from item children
            for child in item:
                if child.tag == "link" and child.text:
                    url = child.text.strip()
                    break

        if not url or url in seen_urls:
            continue

        raw_title = (item.findtext("title") or "").strip()
        if not raw_title or raw_title.startswith("View all"):
            continue

        # Parse "Company Name: Job Title" format
        m = _WWR_TITLE_RE.match(raw_title)
        if m:
            company = m.group(1).strip()
            title = m.group(2).strip()
        else:
            company = ""
            title = raw_title

        pub_date_raw = item.findtext("pubDate") or ""
        dt = _parse_rss_date(pub_date_raw)
        if not _is_recent(dt):
            continue

        # Location from <region> element if present
        region_el = item.find("{https://weworkremotely.com/}region") or item.find("region")
        location = region_el.text.strip() if (region_el is not None and region_el.text) else "Remote"

        seen_urls.add(url)
        jobs.append(_make_job(
            source="We Work Remotely",
            title=title,
            company=company,
            location=location,
            url=url,
            date_posted=_iso_date(dt),
            salary="",
        ))

    filtered = filter_jobs(jobs)
    log.info("We Work Remotely: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── Jooble ─────────────────────────────────────────

_JOOBLE_QUERIES = [
    "IT Director",
    "IT Manager",
    "Director of Infrastructure",
    "Head of IT",
    "VP of IT",
]

_JOOBLE_LOCATIONS = [
    "Remote, USA",
    "Jacksonville, FL",
]


def fetch_jooble() -> list[dict]:
    api_key = os.environ.get(_JOOBLE_KEY_ENV, "").strip()
    if not api_key:
        log.warning("JOOBLE_API_KEY not set — skipping Jooble")
        return []

    endpoint = f"https://jooble.org/api/{api_key}"
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for query in _JOOBLE_QUERIES:
        for location in _JOOBLE_LOCATIONS:
            try:
                resp = requests.post(
                    endpoint,
                    json={
                        "keywords": query,
                        "location": location,
                        "datecreated": str(_CUTOFF_DAYS),
                        "page": "1",
                    },
                    headers={
                        "User-Agent": _UA,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning("Jooble query %r / %r failed: %s", query, location, exc)
                continue

            for item in data.get("jobs", []):
                url = (item.get("link") or "").strip()
                if not url or url in seen_urls:
                    continue

                dt = _parse_iso(item.get("updated", ""))
                if not _is_recent(dt):
                    continue

                seen_urls.add(url)
                jobs.append(_make_job(
                    source="Jooble",
                    title=item.get("title", ""),
                    company=item.get("company", ""),
                    location=item.get("location", "") or location,
                    url=url,
                    date_posted=_iso_date(dt),
                    salary=item.get("salary", "") or "",
                ))

    filtered = filter_jobs(jobs)
    log.info("Jooble: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── JSearch (Google Jobs via RapidAPI) ─────────────

_RAPIDAPI_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
    "X-RapidAPI-Host": "jsearch.p.rapidapi.com",  # overridden per fetcher
}

_JSEARCH_REMOTE_QUERIES = [
    "IT Director",
    "IT Manager",
    "Director of Infrastructure",
    "Head of IT",
    "VP of IT",
]

_JSEARCH_JAX_QUERIES = [
    "IT Director Jacksonville FL",
    "IT Manager Jacksonville FL",
    "Director of IT Jacksonville FL",
]


def fetch_jsearch() -> list[dict]:
    api_key = os.environ.get(_RAPIDAPI_KEY_ENV, "").strip()
    if not api_key:
        log.warning("RAPIDAPI_KEY not set — skipping JSearch")
        return []

    headers = {
        **_RAPIDAPI_HEADERS,
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    def _run_query(query: str, remote_only: bool) -> None:
        params = {
            "query": query,
            "page": "1",
            "num_pages": "1",
            "date_posted": "week",
            "employment_types": "FULLTIME",
        }
        if remote_only:
            params["remote_jobs_only"] = "true"

        try:
            resp = requests.get(
                "https://jsearch.p.rapidapi.com/search",
                params=params,
                headers=headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("JSearch query %r failed: %s", query, exc)
            return

        for item in data.get("data", []):
            url = (item.get("job_apply_link") or "").strip()
            if not url or url in seen_urls:
                continue

            dt = _parse_iso(item.get("job_posted_at_datetime_utc", ""))
            if not _is_recent(dt):
                continue

            if item.get("job_is_remote"):
                location = "Remote"
            else:
                city = item.get("job_city") or ""
                state = item.get("job_state") or ""
                location = ", ".join(p for p in [city, state] if p) or item.get("job_country", "")

            seen_urls.add(url)
            jobs.append(_make_job(
                source="JSearch (Google Jobs)",
                title=item.get("job_title", ""),
                company=item.get("employer_name", ""),
                location=location,
                url=url,
                date_posted=_iso_date(dt),
                salary=_salary_str(
                    item.get("job_min_salary"),
                    item.get("job_max_salary"),
                    item.get("job_salary_period"),
                ),
            ))

    for q in _JSEARCH_REMOTE_QUERIES:
        _run_query(q, remote_only=True)
    for q in _JSEARCH_JAX_QUERIES:
        _run_query(q, remote_only=False)

    filtered = filter_jobs(jobs)
    log.info("JSearch: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── JobsPipe (100+ boards via RapidAPI) ─────────────

_JOBSPIPE_REMOTE_QUERIES = [
    "IT Director",
    "IT Manager",
    "Director of Infrastructure",
    "Head of IT",
    "VP of IT",
]

_JOBSPIPE_JAX_QUERIES = [
    ("IT Director",          "Jacksonville, FL"),
    ("IT Manager",           "Jacksonville, FL"),
    ("Director of IT",       "Jacksonville, FL"),
]


def fetch_jobspipe() -> list[dict]:
    api_key = os.environ.get(_RAPIDAPI_KEY_ENV, "").strip()
    if not api_key:
        log.warning("RAPIDAPI_KEY not set — skipping JobsPipe")
        return []

    headers = {
        **_RAPIDAPI_HEADERS,
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "jobspipe.p.rapidapi.com",
    }
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    def _extract_url(item: dict) -> str:
        for key in ("url", "applyUrl", "apply_url", "jobUrl", "job_url", "link", "externalUrl"):
            val = item.get(key, "")
            if val:
                return str(val).strip()
        return ""

    def _extract_date(item: dict) -> str:
        for key in ("postedAt", "posted_at", "datePosted", "date_posted", "publishedAt", "published_at", "createdAt"):
            val = item.get(key, "")
            if val:
                return str(val).strip()
        return ""

    def _extract_salary(item: dict) -> str:
        for key in ("salary", "salaryRange", "salary_range", "compensation"):
            val = item.get(key, "")
            if val:
                return str(val).strip()
        return ""

    def _run_query(query: str, location: str = "") -> None:
        params: dict = {"q": query, "limit": "20", "datePosted": str(_CUTOFF_DAYS)}
        if location:
            params["location"] = location

        try:
            resp = requests.get(
                "https://jobspipe.p.rapidapi.com/jobs",
                params=params,
                headers=headers,
                timeout=_TIMEOUT,
            )
            # Log raw field names from first item so we can verify mapping
            if resp.status_code == 200:
                raw = resp.json()
                items = raw if isinstance(raw, list) else raw.get("jobs", raw.get("data", raw.get("results", [])))
                if items and not jobs:
                    log.debug("JobsPipe first item keys: %s", list(items[0].keys()))
            resp.raise_for_status()
        except Exception as exc:
            log.warning("JobsPipe query %r / %r failed: %s", query, location, exc)
            return

        raw = resp.json()
        items = raw if isinstance(raw, list) else raw.get("jobs", raw.get("data", raw.get("results", [])))

        for item in items:
            url = _extract_url(item)
            if not url or url in seen_urls:
                continue

            date_raw = _extract_date(item)
            dt = _parse_iso(date_raw)
            if not _is_recent(dt):
                continue

            loc = (
                item.get("location") or item.get("jobLocation") or item.get("job_location")
                or location or ""
            )

            seen_urls.add(url)
            jobs.append(_make_job(
                source="JobsPipe",
                title=item.get("title") or item.get("jobTitle") or item.get("job_title") or "",
                company=item.get("company") or item.get("companyName") or item.get("employer") or "",
                location=str(loc).strip(),
                url=url,
                date_posted=_iso_date(dt),
                salary=_extract_salary(item),
            ))

    for q in _JOBSPIPE_REMOTE_QUERIES:
        _run_query(q)
    for q, loc in _JOBSPIPE_JAX_QUERIES:
        _run_query(q, loc)

    filtered = filter_jobs(jobs)
    log.info("JobsPipe: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── orchestrator ───────────────────────────────────


def fetch_all_external() -> list[dict]:
    """
    Fetch from all external boards and return a single de-duplicated list.

    De-duplication is by application_url — if the same URL appears on multiple
    boards, the first occurrence wins.
    """
    all_jobs: list[dict] = []
    seen_urls: set[str] = set()

    for fetcher in (
        fetch_himalayas,
        fetch_remotive,
        fetch_wwr,
        fetch_jooble,
        fetch_jsearch,
        fetch_jobspipe,
    ):
        try:
            batch = fetcher()
        except Exception as exc:
            log.error("External fetcher %s raised unexpectedly: %s", fetcher.__name__, exc)
            batch = []

        for job in batch:
            url = job["application_url"]
            if url not in seen_urls:
                seen_urls.add(url)
                all_jobs.append(job)

    log.info("External feeds total: %d jobs (after cross-board dedup)", len(all_jobs))
    return all_jobs
