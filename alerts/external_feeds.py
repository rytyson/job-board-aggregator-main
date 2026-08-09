"""
External job board fetchers: Himalayas, Remotive, We Work Remotely, Jooble,
JSearch (Google Jobs), JobsPipe, Employ Florida, Indeed (via Apify), Dice
(via Apify).

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
                           "Jooble" | "JSearch (Google Jobs)" | "JobsPipe" |
                           "Employ Florida" | "Indeed" | "Dice"
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


def _parse_relative_date(raw: str) -> datetime | None:
    """Parse relative date strings like 'Today', '3 days ago', 'Yesterday', '30+ days ago'."""
    if not raw:
        return None
    text = raw.strip().lower()
    now = _now()
    if text in ("today", "just posted", "new", "just now", "active today"):
        return now
    if "yesterday" in text:
        return now - timedelta(days=1)
    m = re.match(r"(\d+)\+?\s*hour", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.match(r"(\d+)\+?\s*day", text)
    if m:
        return now - timedelta(days=int(m.group(1)))
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


# ─────────────────────────── JSearch (Google Jobs — OpenWeb Ninja direct API) ─

_OPENWEBNINJA_KEY_ENV = "OPENWEBNINJA_API_KEY"

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
    api_key = os.environ.get(_OPENWEBNINJA_KEY_ENV, "").strip()
    if not api_key:
        log.warning("OPENWEBNINJA_API_KEY not set — skipping JSearch")
        return []

    headers = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "x-api-key": api_key,
    }
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    def _run_query(query: str, remote_only: bool) -> None:
        params: dict = {"query": query, "num_pages": "1"}
        if remote_only:
            params["work_from_home"] = "true"

        try:
            resp = requests.get(
                "https://api.openwebninja.com/jsearch/search-v2",
                params=params,
                headers=headers,
                timeout=30,
            )
            if not resp.ok:
                log.warning(
                    "JSearch query %r → HTTP %s — body: %s",
                    query, resp.status_code, resp.text[:300],
                )
                return
            data = resp.json()
        except Exception as exc:
            log.warning("JSearch query %r failed: %s", query, exc)
            return

        if not isinstance(data, dict):
            log.warning("JSearch query %r — unexpected response type %s: %s", query, type(data).__name__, str(data)[:200])
            return

        # v2 response: {"status":..., "data": {"data": [jobs]}} — one extra nesting level
        inner = data.get("data")
        if isinstance(inner, list):
            raw_items = inner
        elif isinstance(inner, dict):
            raw_items = inner.get("data") or inner.get("jobs") or inner.get("results") or []
        else:
            raw_items = []

        if not isinstance(raw_items, list):
            log.warning("JSearch query %r — could not find job list; top keys: %s, inner keys: %s",
                        query, list(data.keys()), list(inner.keys()) if isinstance(inner, dict) else inner)
            return
        if raw_items and not jobs:
            log.debug("JSearch first item keys: %s", list(raw_items[0].keys()) if isinstance(raw_items[0], dict) else type(raw_items[0]).__name__)

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            url = (item.get("job_apply_link") or "").strip()
            if not url or url in seen_urls:
                continue

            # v2 may return timestamp or ISO string
            raw_date = item.get("job_posted_at_datetime_utc") or item.get("job_posted_at", "")
            if isinstance(raw_date, (int, float)):
                from datetime import datetime, timezone as _tz
                dt = datetime.fromtimestamp(raw_date, tz=_tz.utc)
            else:
                dt = _parse_iso(str(raw_date))
            if not _is_recent(dt):
                continue

            work_arr = item.get("work_arrangement", "")
            is_remote = (
                item.get("job_is_remote")
                or (isinstance(work_arr, str) and "remote" in work_arr.lower())
                or (isinstance(work_arr, list) and any("remote" in str(w).lower() for w in work_arr))
            )
            if is_remote:
                location = "Remote"
            else:
                location = (
                    item.get("job_location")
                    or ", ".join(p for p in [item.get("job_city", ""), item.get("job_state", "")] if p)
                    or item.get("job_country", "")
                )

            seen_urls.add(url)
            jobs.append(_make_job(
                source="JSearch (Google Jobs)",
                title=item.get("job_title", ""),
                company=item.get("employer_name", ""),
                location=location,
                url=url,
                date_posted=_iso_date(dt) if dt else "",
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


# ─────────────────────────── JobsPipe (30+ ATS boards — direct API) ──────────

_JOBSPIPE_KEY_ENV = "JOBSPIPE_API_KEY"

_JOBSPIPE_TITLES = [
    "IT Director",
    "IT Manager",
    "Director of Infrastructure",
    "Director of IT",
    "Head of IT",
    "VP of IT",
    "VP IT",
    "Chief Information Officer",
    "CIO",
]


def fetch_jobspipe() -> list[dict]:
    api_key = os.environ.get(_JOBSPIPE_KEY_ENV, "").strip()
    if not api_key:
        log.warning("JOBSPIPE_API_KEY not set — skipping JobsPipe")
        return []

    headers = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    def _run_search(body: dict, label: str) -> None:
        try:
            resp = requests.post(
                "https://api.jobspipe.dev/v1/jobs/search",
                json=body,
                headers=headers,
                timeout=_TIMEOUT,
            )
            if not resp.ok:
                log.warning(
                    "JobsPipe %r → HTTP %s — body: %s",
                    label, resp.status_code, resp.text[:300],
                )
                return
            raw = resp.json()
        except Exception as exc:
            log.warning("JobsPipe %r failed: %s", label, exc)
            return

        job_list = raw if isinstance(raw, list) else raw.get("jobs", raw.get("data", raw.get("results", [])))
        if job_list and not jobs:
            log.debug("JobsPipe first item keys: %s", list(job_list[0].keys()))

        for item in job_list:
            url = (item.get("url") or item.get("final_url") or "").strip()
            if not url or url in seen_urls:
                continue

            dt = _parse_iso(item.get("date_posted", ""))
            if not _is_recent(dt):
                continue

            is_remote = item.get("remote", False)
            loc_val = item.get("location")
            if is_remote:
                location = "Remote"
            elif isinstance(loc_val, dict):
                cities = loc_val.get("cities") or []
                country = loc_val.get("country", "")
                location = ", ".join(filter(None, [cities[0] if cities else "", country]))
            else:
                location = str(loc_val or "").strip()

            company_val = item.get("company")
            company = (
                company_val if isinstance(company_val, str)
                else (company_val.get("name", "") if isinstance(company_val, dict) else "")
            )

            min_sal = item.get("min_annual_salary")
            max_sal = item.get("max_annual_salary")
            salary = item.get("salary_string") or _salary_str(min_sal, max_sal, "year")

            seen_urls.add(url)
            jobs.append(_make_job(
                source="JobsPipe",
                title=item.get("job_title", ""),
                company=company,
                location=location,
                url=url,
                date_posted=_iso_date(dt) if dt else "",
                salary=salary,
            ))

    _run_search({
        "job_title_or": _JOBSPIPE_TITLES,
        "remote": True,
        "job_country_code_or": ["US"],
        "posted_at_max_age_days": _CUTOFF_DAYS,
        "limit": 50,
    }, label="remote US")

    _run_search({
        "job_title_or": _JOBSPIPE_TITLES,
        "job_location_or": ["Jacksonville, FL"],
        "job_country_code_or": ["US"],
        "posted_at_max_age_days": _CUTOFF_DAYS,
        "limit": 20,
    }, label="Jacksonville FL")

    filtered = filter_jobs(jobs)
    log.info("JobsPipe: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── Employ Florida (state job board) ────────────────

_EMPLOYFLORIDA_SEARCHES = [
    ("IT Director",          "Jacksonville FL"),
    ("IT Manager",           "Jacksonville FL"),
    ("Director of IT",       "Jacksonville FL"),
    ("VP of IT",             "Jacksonville FL"),
    ("IT Director",          "Florida"),
    ("IT Manager",           "Florida"),
]


def fetch_employflorida() -> list[dict]:
    """Scrape Florida's state job board using a headless browser (Playwright)."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("playwright not installed — skipping Employ Florida")
        return []

    _BASE = "https://www.employflorida.com"
    _SEARCH_PAGE = f"{_BASE}/vosnet/JobBanks/JobSearchCriteriaQuick.aspx?nf=1"
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()
        page.on("dialog", lambda d: (log.info("Employ Florida JS dialog: %s", d.message), d.dismiss()))
        first = True
        first_diag = [True]

        for keyword, location in _EMPLOYFLORIDA_SEARCHES:
            try:
                # Load the search form fresh each time (VOS requires proper ViewState)
                log.info("Employ Florida '%s'/'%s': loading search page", keyword, location)
                page.goto(_SEARCH_PAGE, wait_until="domcontentloaded", timeout=30_000)
                log.info("Employ Florida '%s'/'%s': page loaded, URL=%s", keyword, location, page.url)
                # Wait for the Quick Search form to be ready before filling
                page.wait_for_selector('#univsearchtxtkeywordquick', timeout=10_000)
                log.info("Employ Florida '%s'/'%s': form ready, submitting via JS", keyword, location)

                first = False

                # Select "Keyword Type" FIRST — its change handler resets/clears the
                # keyword text field, which is why every prior run silently searched
                # with a blank keyword and got back an unfiltered "browse all" list.
                # Fill the text fields only AFTER that reset has already happened.
                page.evaluate("""([kw, loc]) => {
                    const radios = document.querySelectorAll('input[name="ctl00$Main_content$rblKeywordType"]');
                    if (radios.length && !Array.from(radios).some(r => r.checked)) {
                        radios[0].checked = true;
                        radios[0].dispatchEvent(new Event('change', {bubbles: true}));
                        radios[0].dispatchEvent(new Event('click', {bubbles: true}));
                    }
                    const fire = (el, val) => {
                        el.value = val;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    };
                    fire(document.getElementById('univsearchtxtkeywordquick'), kw);
                    fire(document.getElementById('ctl00_Main_content_univsearchlocation'), loc);
                }""", [keyword, location])

                # Read back the actual field values right before submit — confirms
                # nothing downstream (radio handler, validator) wiped them again.
                pre_submit = page.evaluate("""() => ({
                    kw: document.getElementById('univsearchtxtkeywordquick').value,
                    loc: document.getElementById('ctl00_Main_content_univsearchlocation').value
                })""")
                log.info("Employ Florida '%s'/'%s': pre-submit field values: %s",
                         keyword, location, pre_submit)

                if first_diag[0]:
                    btn_info = page.evaluate("""() => {
                        const btn = document.getElementById('ctl00_Main_content_btnSearch2');
                        const r = btn.getBoundingClientRect();
                        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                        const atPoint = document.elementFromPoint(cx, cy);
                        const form = btn.closest('form');
                        return {
                            btnOnclick: btn.getAttribute('onclick'),
                            btnDisabled: btn.disabled,
                            btnRect: {x: r.left, y: r.top, w: r.width, h: r.height},
                            btnVisible: r.width > 0 && r.height > 0,
                            atPointTag: atPoint ? atPoint.tagName : null,
                            atPointId: atPoint ? atPoint.id : null,
                            isSameElement: atPoint === btn,
                            formAction: form ? form.action : null,
                            formOnsubmit: form ? form.getAttribute('onsubmit') : null
                        };
                    }""")
                    log.info("Employ Florida button diag: %s", btn_info)
                    first_diag[0] = False

                # The button is visually covered by a DIV at its screen coordinates, so any
                # coordinate-based click (Playwright's, even force:true) hits the DIV instead
                # of the button. Dispatch the click event directly on the button node — this
                # bypasses elementFromPoint hit-testing and fires the real onclick handler
                # (checkForm() -> showPleaseWait() -> native form submit).
                page.evaluate("""() => {
                    const btn = document.getElementById('ctl00_Main_content_btnSearch2');
                    btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                }""")
                log.info("Employ Florida '%s'/'%s': submit dispatched, waiting", keyword, location)

                # Don't require a specific URL — just wait a beat for the postback/redirect,
                # then log what actually happened (URL, title, any validation errors).
                page.wait_for_timeout(5_000)
                diag = page.evaluate("""() => ({
                    url: window.location.href,
                    title: document.title,
                    errorText: (() => {
                        const el = document.querySelector('.validation-summary-errors, [id*=Validation], [class*=error], [class*=Error]');
                        return el ? el.innerText.trim().slice(0, 300) : null;
                    })(),
                    keywordTypeChecked: (() => {
                        const r = document.querySelector('input[name="ctl00$Main_content$rblKeywordType"]:checked');
                        return r ? r.value : null;
                    })()
                })""")
                log.info("Employ Florida '%s'/'%s' post-submit: url=%s title=%s error=%s kwType=%s",
                         keyword, location, diag.get('url'), diag.get('title'), diag.get('errorText'),
                         diag.get('keywordTypeChecked'))

                log.info("Employ Florida '%s'/'%s' results URL: %s", keyword, location, page.url)

                # Extract job detail links from the results page
                result_data = page.evaluate("""() => {
                    const items = [];
                    document.querySelectorAll('a[href*="JobDetails"], a[href*="jobdetail"]').forEach(a => {
                        const row = a.closest('tr') || a.closest('li') || a.closest('div[class*="job"]') || a.parentElement;
                        items.push({
                            title: a.innerText.trim(),
                            url: a.href,
                            rowText: row ? row.innerText.trim() : ''
                        });
                    });
                    const allHrefs = Array.from(document.querySelectorAll('a[href*="vosnet/JobBanks"]'))
                                         .slice(0,5).map(a => a.href);
                    return {items, totalLinks: document.querySelectorAll('a').length, sampleJobHrefs: allHrefs};
                }""")

                log.info("Employ Florida '%s'/'%s': %d job links (%d total) sample: %s",
                         keyword, location,
                         len(result_data.get('items', [])),
                         result_data.get('totalLinks', 0),
                         result_data.get('sampleJobHrefs', [])[:2])
                for it in result_data.get('items', [])[:3]:
                    log.info("Employ Florida item sample: title=%r url=%r rowText=%r",
                             it.get('title'), it.get('url'), it.get('rowText', '')[:200])

                for item in result_data.get('items', []):
                    url = item.get("url", "").strip()
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = item.get("title", "").strip()
                    row_text = item.get("rowText", "")
                    lines = [ln.strip() for ln in row_text.splitlines() if ln.strip()]
                    # Heuristic: line 0 = title, line 1 = company, line 2 = location, etc.
                    company = lines[1] if len(lines) > 1 else ""
                    loc_str = lines[2] if len(lines) > 2 else location

                    jobs.append(_make_job(
                        source="Employ Florida",
                        title=title,
                        company=company,
                        location=loc_str,
                        url=url,
                        date_posted="",
                        salary="",
                    ))

            except PWTimeout:
                log.warning("Employ Florida '%s'/'%s' timed out", keyword, location)
            except Exception as exc:
                log.warning("Employ Florida '%s'/'%s' failed: %s", keyword, location, exc)

        browser.close()

    filtered = filter_jobs(jobs)
    log.info("Employ Florida: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── Indeed (via Apify) ──────────────────────────────
#
# Indeed has no public job-search API (only employer-side posting APIs). We use
# Apify's "misceres/indeed-scraper" actor, a managed scraper that handles
# Indeed's bot detection for us — pay-per-result, from $3.00/1,000 listings.
# One APIFY_API_TOKEN covers every Apify actor on the account (Indeed + Dice).

_APIFY_TOKEN_ENV = "APIFY_API_TOKEN"
_APIFY_INDEED_ACTOR = "misceres~indeed-scraper"

# Indeed's real search box supports boolean OR syntax in the title query —
# this actor passes `position` straight through as Indeed's own "q" param.
_INDEED_QUERY = (
    '"IT Director" OR "IT Manager" OR "Director of Infrastructure" OR '
    '"Director of IT" OR "Head of IT" OR "VP of IT" OR '
    '"Chief Information Officer" OR "CIO"'
)

_INDEED_SEARCHES = [
    (_INDEED_QUERY, "Remote"),
    (_INDEED_QUERY, "Jacksonville, FL"),
]


def fetch_indeed() -> list[dict]:
    api_token = os.environ.get(_APIFY_TOKEN_ENV, "").strip()
    if not api_token:
        log.warning("APIFY_API_TOKEN not set — skipping Indeed")
        return []

    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for query, location in _INDEED_SEARCHES:
        try:
            resp = requests.post(
                f"https://api.apify.com/v2/acts/{_APIFY_INDEED_ACTOR}/run-sync-get-dataset-items",
                params={"token": api_token},
                json={
                    "position": query,
                    "location": location,
                    "country": "US",
                    "maxItemsPerSearch": 50,
                },
                headers={"Content-Type": "application/json"},
                timeout=90,
            )
            if not resp.ok:
                log.warning("Indeed (Apify) %r → HTTP %s — body: %s",
                            location, resp.status_code, resp.text[:300])
                continue
            items = resp.json()
            if not isinstance(items, list):
                log.warning("Indeed (Apify) %r → unexpected response shape: %s",
                             location, str(items)[:300])
                continue
        except Exception as exc:
            log.warning("Indeed (Apify) %r failed: %s", location, exc)
            continue

        if items:
            log.debug("Indeed (Apify) first item keys: %s", list(items[0].keys()))

        for item in items:
            url = (item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue

            dt = _parse_relative_date(item.get("postedAt", ""))
            if not _is_recent(dt):
                continue

            salary_val = item.get("salary")
            salary = salary_val if isinstance(salary_val, str) else ""

            seen_urls.add(url)
            jobs.append(_make_job(
                source="Indeed",
                title=item.get("positionName", ""),
                company=item.get("company", ""),
                location=item.get("location", "") or location,
                url=url,
                date_posted=_iso_date(dt) if dt else "",
                salary=salary,
            ))

    filtered = filter_jobs(jobs)
    log.info("Indeed: %d raw → %d after filter", len(jobs), len(filtered))
    return filtered


# ─────────────────────────── Dice (via Apify) ─────────────────────────────────
#
# Dice has no public API either. Uses Apify's "worldunboxer/dice-jobs-scraper"
# actor — pay-per-result, from $0.07/1,000 results (much cheaper than Indeed's
# actor). Same APIFY_API_TOKEN as fetch_indeed().

_APIFY_DICE_ACTOR = "worldunboxer~dice-jobs-scraper"

_DICE_TITLES = [
    "IT Director",
    "IT Manager",
    "Director of Infrastructure",
    "Director of IT",
    "VP of IT",
    "CIO",
]

_DICE_SEARCHES = (
    [(t, "Remote") for t in _DICE_TITLES]
    + [(t, "Jacksonville, FL") for t in ("IT Director", "IT Manager", "Director of IT")]
)


def fetch_dice() -> list[dict]:
    api_token = os.environ.get(_APIFY_TOKEN_ENV, "").strip()
    if not api_token:
        log.warning("APIFY_API_TOKEN not set — skipping Dice")
        return []

    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for keyword, location in _DICE_SEARCHES:
        try:
            resp = requests.post(
                f"https://api.apify.com/v2/acts/{_APIFY_DICE_ACTOR}/run-sync-get-dataset-items",
                params={"token": api_token},
                json={
                    "keyword": keyword,
                    "location": location,
                    "radius": 0 if location == "Remote" else 50,
                    "unit": "mi",
                    "limit": 20,
                },
                headers={"Content-Type": "application/json"},
                timeout=90,
            )
            if not resp.ok:
                log.warning("Dice (Apify) %r/%r → HTTP %s — body: %s",
                            keyword, location, resp.status_code, resp.text[:300])
                continue
            items = resp.json()
            if not isinstance(items, list):
                log.warning("Dice (Apify) %r/%r → unexpected response shape: %s",
                             keyword, location, str(items)[:300])
                continue
        except Exception as exc:
            log.warning("Dice (Apify) %r/%r failed: %s", keyword, location, exc)
            continue

        if items:
            log.debug("Dice (Apify) first item keys: %s", list(items[0].keys()))

        for item in items:
            url = (item.get("details_page_url") or item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue

            dt = _parse_relative_date(item.get("posted_date", ""))
            if not _is_recent(dt):
                continue

            seen_urls.add(url)
            jobs.append(_make_job(
                source="Dice",
                title=item.get("title", ""),
                company=item.get("company", ""),
                location=item.get("location", "") or location,
                url=url,
                date_posted=_iso_date(dt) if dt else "",
                salary=item.get("salary", "") or "",
            ))

    filtered = filter_jobs(jobs)
    log.info("Dice: %d raw → %d after filter", len(jobs), len(filtered))
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
        fetch_employflorida,
        fetch_indeed,
        fetch_dice,
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
