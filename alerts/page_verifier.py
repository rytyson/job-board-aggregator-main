"""
Playwright-based job posting verification.

Renders each posting in headless Chromium to extract verified data:
  is_live           — True if the job is still accepting applications
  location_verified — actual location extracted from the rendered page
  salary_posted     — salary range if explicitly posted
  date_posted       — YYYY-MM-DD of actual posting date
  liveness_checked_at — ISO-8601 timestamp of this check

Workday jobs use the CXS JSON API for liveness + date (fast, 100% reliable).
All other platforms use Playwright to fully render the JS SPA before inspection.

iCIMS rules (strict):
  - notFound=1 in URL → closed immediately, no page load needed
  - /jobs/search in final URL → closed (iCIMS search-page redirect = removed)
  - Location ALWAYS verified from the rendered page (chunk remote/location unreliable)
  - Page loaded but location unverifiable → excluded (safe over sorry)
  - Timeout/network error → kept with ⚠ flag (technical issue, not job removal)

Public API:
    verify_all_jobs(jobs: list[dict], max_concurrent: int = 5) -> list[dict]
"""

import asyncio
import json
import logging
import re
from datetime import date, timedelta, datetime, timezone

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

log = logging.getLogger(__name__)


# ── Closed-job detection phrases ──────────────────────────────────────────────

_CLOSED_PHRASES = [
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
    # iCIMS explicit "not found" message
    "the requested job could not be found",
    "job that you were looking for either does not exist or is no longer open",
    # Workday (fallback if CXS API fails)
    "page you are looking for doesn't exist",
    "page you are looking for does not exist",
    "this page no longer exists",
    "this job requisition is no longer open",
    "this requisition is no longer open",
    # Generic 404-style pages returned as HTTP 200
    "404 - page not found",
    "page not found",
    "page cannot be found",
    "this page could not be found",
    "we couldn't find this page",
    "oops! the page",
    "oops, page not found",
]

# Page titles that indicate an error/empty page (no job content)
_GENERIC_TITLES = {
    '', 'greenhouse', 'lever', 'ashby', 'bamboohr', 'icims',
    'jobs', 'careers', 'job search', 'job board',
    'error', '404', 'page not found', 'not found',
    'icims careers portal',   # iCIMS SPA shell title (before JS loads)
}

# ── Location filtering ────────────────────────────────────────────────────────

# Remote always accepted regardless of any state suffix
_REMOTE_RE = re.compile(
    r'\bremote\b|\bwork from home\b|\bwfh\b|\banywhere\b|\bvirtual\b',
    re.IGNORECASE,
)
# "City, XX" where XX is a US state NOT FL — onsite outside target area
_US_NON_FL_STATE_RE = re.compile(
    r',\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|'
    r'MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|'
    r'UT|VT|VA|WA|WV|WI|WY|DC)\b',
    re.IGNORECASE,
)
# International country names
_INTL_COUNTRY_RE = re.compile(
    r'\b(?:canada|uk|united kingdom|ireland|australia|india|philippines|'
    r'germany|france|spain|italy|netherlands|brazil|mexico|singapore|'
    r'japan|south korea|israel|poland|sweden|denmark|norway|portugal|'
    r'switzerland|austria|belgium|czech|hungary|romania|south africa|'
    r'nigeria|egypt|uae|pakistan|lebanon|colombia|argentina|'
    r'new zealand|taiwan|vietnam|thailand|malaysia|peru|chile|'
    r'kenya|ghana|senegal|uganda|ethiopia)\b',
    re.IGNORECASE,
)

# ── Salary extraction ─────────────────────────────────────────────────────────

_SALARY_LABELS = [
    "salary", "compensation", "pay range", "base pay",
    "annual salary", "total compensation", "wage",
    "base salary", "salary range", "starting salary",
]
_SALARY_RE = re.compile(
    r'\$\s*(\d{1,3}(?:,\d{3})*|\d+)\s*[kK]?\s*(?:[-–—]\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)\s*[kK]?)?'
)

# ── Workday URL pattern ───────────────────────────────────────────────────────

_WD_URL_RE = re.compile(
    r'https://([^.]+)(\.wd\d+\.myworkdayjobs\.com)/([^/]+)/job/.+/[^_/]+_([A-Za-z0-9\-]+?)(?:/|\?|$)',
    re.IGNORECASE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_date(raw: str) -> str | None:
    raw = raw.strip().rstrip('.')
    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
        return raw
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', raw)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    for fmt in (
        '%B %d, %Y', '%B %d %Y', '%b %d, %Y', '%b %d %Y',
        '%d %B %Y', '%d %b %Y', '%B %d,%Y',
    ):
        try:
            return datetime.strptime(raw, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return None


def _extract_date(text: str) -> str | None:
    m = re.search(r'(\d+)\s+days?\s+ago', text, re.IGNORECASE)
    if m and int(m.group(1)) <= 365:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(
        r'posted(?:\s+on)?\s*[:\-]?\s*'
        r'(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}'
        r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w{0,6}\.?\s+\d{1,2},?\s+\d{4})',
        text, re.IGNORECASE,
    )
    if m:
        return _normalise_date(m.group(1))
    return None



# Lever / BambooHR put annual salary without a nearby label — just dollars + "a year"
_SALARY_ANNUAL_RE = re.compile(
    r'\$\s*(\d{1,3}(?:,\d{3})*)\s*[kK]?\s*[-–—]\s*\$?\s*(\d{1,3}(?:,\d{3})*)\s*[kK]?\s*'
    r'(?:a\s+year|per\s+year|annually|\/year|\/yr)\b',
    re.IGNORECASE,
)
_SALARY_SINGLE_ANNUAL_RE = re.compile(
    r'\$\s*(\d{1,3}(?:,\d{3})*)\s*[kK]?\s*'
    r'(?:a\s+year|per\s+year|annually|\/year|\/yr)\b',
    re.IGNORECASE,
)


def _parse_salary_match(lo_str: str, hi_str: str | None) -> str | None:
    try:
        lo = float(lo_str.replace(',', ''))
        if lo < 1000:
            lo *= 1000
        lo = int(lo)
        if lo < 25_000 or lo > 2_000_000:
            return None
        if hi_str:
            hi = float(hi_str.replace(',', ''))
            if hi < 1000:
                hi *= 1000
            hi = int(hi)
            if hi < lo:
                hi, lo = lo, hi
            if hi > 2_000_000:
                return None
            return f"${lo:,} – ${hi:,}"
        return f"${lo:,}"
    except (ValueError, TypeError):
        return None


def _extract_salary(text: str) -> str | None:
    # Pass 1: "$X – $Y a year" pattern (Lever, BambooHR, others without label)
    m = _SALARY_ANNUAL_RE.search(text)
    if m:
        result = _parse_salary_match(m.group(1), m.group(2))
        if result:
            return result

    # Pass 2: "$X a year" (single value, annual)
    m = _SALARY_SINGLE_ANNUAL_RE.search(text)
    if m:
        result = _parse_salary_match(m.group(1), None)
        if result:
            return result

    # Pass 3: salary label nearby, then dollar amount
    text_lower = text.lower()
    for label in _SALARY_LABELS:
        pos = text_lower.find(label)
        if pos == -1:
            continue
        chunk = text[max(0, pos - 20):pos + 400]
        for m in _SALARY_RE.finditer(chunk):
            result = _parse_salary_match(m.group(1), m.group(2))
            if result:
                return result
    return None


def _parse_jsonld(data: dict) -> dict:
    out: dict = {}

    dp = data.get('datePosted') or data.get('dateCreated')
    if dp:
        d = _normalise_date(str(dp)[:10])
        if d:
            out['date_posted'] = d

    vt = data.get('validThrough')
    if vt:
        try:
            if date.fromisoformat(str(vt)[:10]) < date.today():
                out['expired'] = True
        except (ValueError, TypeError):
            pass

    loc_obj = data.get('jobLocation') or data.get('applicantLocationRequirements')
    if isinstance(loc_obj, list):
        loc_obj = loc_obj[0] if loc_obj else None
    if isinstance(loc_obj, dict):
        addr = loc_obj.get('address') or loc_obj
        if isinstance(addr, dict):
            city    = addr.get('addressLocality', '').strip()
            region  = addr.get('addressRegion', '').strip()
            country = addr.get('addressCountry', '').strip()
            if country and country not in ('US', 'USA', 'United States'):
                out['location_verified'] = f"{city}, {country}".strip(', ')
            elif city and region:
                out['location_verified'] = f"{city}, {region}"
            elif region:
                out['location_verified'] = region
            elif country in ('US', 'USA', 'United States'):
                out['location_verified'] = 'United States'

    if 'remote' in str(data.get('jobLocationType') or '').lower():
        out['location_verified'] = 'Remote'

    base = data.get('baseSalary') or data.get('estimatedSalary')
    if isinstance(base, dict):
        val = base.get('value', {})
        if isinstance(val, dict):
            lo = val.get('minValue')
            hi = val.get('maxValue')
            if lo and hi:
                out['salary_posted'] = f"${int(lo):,} – ${int(hi):,}"
            elif lo:
                out['salary_posted'] = f"${int(lo):,}+"
        elif isinstance(val, (int, float)) and val > 0:
            out['salary_posted'] = f"${int(val):,}"

    return out


def _location_is_excluded(loc: str | None) -> bool:
    """
    Return True if the verified location is definitively outside Ryan's target area.
    Target: Remote (US), Jacksonville FL, Florida, or broad United States.
    Remote takes precedence — "Remote, CA" is still accepted.
    """
    if not loc:
        return False
    if _REMOTE_RE.search(loc):
        return False
    if _INTL_COUNTRY_RE.search(loc):
        return True
    # US onsite outside FL — pattern: ", XX" where XX != FL
    m = _US_NON_FL_STATE_RE.search(loc)
    if m:
        if not re.search(r'\bFL\b|\bflorida\b|\bjacksonville\b|\bremote\b', loc, re.IGNORECASE):
            return True
    return False


async def _get_jsonld(frame_or_page) -> dict | None:
    """Return the first JobPosting JSON-LD block on the page (or any frame)."""
    try:
        for script in await frame_or_page.query_selector_all('script[type="application/ld+json"]'):
            try:
                data = json.loads(await script.inner_text())
                for item in (data if isinstance(data, list) else [data]):
                    if isinstance(item, dict) and item.get('@type') in ('JobPosting', 'JobListing'):
                        return item
            except Exception:
                continue
    except Exception:
        pass
    return None


async def _all_text(page) -> str:
    """
    Collect rendered body text from the main frame AND all child frames.
    iCIMS bridge-mode pages sometimes load job content into a child iframe.
    """
    parts: list[str] = []
    try:
        parts.append(await page.inner_text('body'))
    except Exception:
        pass
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            ft = await frame.inner_text('body')
            if ft:
                parts.append(ft)
        except Exception:
            pass
    return '\n'.join(parts)


async def _extract_icims_location(page, body_text: str, page_title: str) -> str | None:
    """
    Extract the real job location from a rendered iCIMS page.
    Tries four methods in priority order.
    """
    # Method 1: Page title — iCIMS often formats as "Title - City, ST - Company"
    for pattern in (
        r'[-–]\s*(Remote|Work from Home|Virtual|Anywhere|WFH)\s*(?:[-–|,]|$)',
        r'[-–]\s*((?:[A-Z][a-z]+(?:[\s\-][A-Z][a-z]+)*),\s*[A-Z]{2})\s*(?:[-–|,]|$)',
        r'[-–]\s*(United States|Nationwide|USA|U\.S\.A?\.?)\s*(?:[-–|,]|$)',
    ):
        m = re.search(pattern, page_title, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # Method 2: "Location" label immediately followed by the value in body text
    # Rendered iCIMS typically looks like: "Location\nChantilly, VA, US"
    m = re.search(
        r'\bLocation\b\s*[\n:]\s*([^\n]{3,100})',
        body_text, re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip()
        # Reject obvious UI noise
        bad_words = ('filter', 'search', 'loading', 'apply', 'click', 'submit',
                     'all locations', 'select', 'type to')
        if not any(w in candidate.lower() for w in bad_words):
            return candidate

    # Method 3: iCIMS DOM selectors (standard and custom layouts)
    for sel in (
        '[data-field="formfield-C_Location"] span',
        '.iCIMS_TableBody tr td + td',
        '[class*="locationTitle"]',
        '[class*="jobLocation"]',
        '[class*="location-text"]',
        '.iCIMS_TableCell',
    ):
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.inner_text()).strip()
                if txt and 3 < len(txt) < 120:
                    return txt
        except Exception:
            pass

    # Method 4: City, STATE or Remote pattern anywhere in body text
    m = re.search(
        r'\b(?:Remote|Work from Home|WFH|Virtual|Nationwide'
        r'|United States|Anywhere in (?:the )?U\.?S\.?A?\.?)\b'
        r'|(?:[A-Z][a-z]{2,}(?:[ \-][A-Z][a-z]+)*),\s*'
        r'(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|'
        r'LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|'
        r'OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b',
        body_text,
    )
    if m:
        return m.group(0).strip()

    return None


# ── Greenhouse embed verifier ─────────────────────────────────────────────────

def _check_greenhouse_embed(url: str, page_html: str, job: dict, checked_at: str) -> dict | None:
    """
    For URLs containing ?gh_jid=ID (Greenhouse job embedded on a company site),
    verify liveness via the Greenhouse boards API.

    Strategy:
      1. Look for 'boards.greenhouse.io/SLUG' in the page HTML (most reliable).
      2. Derive slug from the URL domain as a fallback.

    Returns enriched job dict if liveness can be determined, else None.
    """
    import requests as req
    from urllib.parse import urlparse

    gh_jid = re.search(r'gh_jid=(\d+)', url)
    if not gh_jid:
        return None
    job_id = gh_jid.group(1)

    def _dead() -> dict:
        return {**job, 'is_live': False, 'salary_posted': None,
                'date_posted': None, 'location_verified': None,
                'liveness_checked_at': checked_at}

    def _from_api(data: dict) -> dict:
        loc = (data.get('location') or {}).get('name', '') or ''
        dp = (data.get('updated_at') or '')[:10] or None
        return {**job, 'is_live': True, 'salary_posted': None,
                'date_posted': dp, 'location_verified': loc or None,
                'liveness_checked_at': checked_at}

    def _try_api(slug: str) -> dict | None:
        api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        try:
            r = req.get(api, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
            if r.status_code == 200:
                return _from_api(r.json())
            if r.status_code == 404:
                return _dead()
        except Exception:
            pass
        return None

    # Method 1: Greenhouse embed script — ?for=COMPANY_SLUG (most reliable)
    # e.g. boards.greenhouse.io/embed/job_board/js?for=oasishealthpartners
    m = re.search(
        r'boards\.greenhouse\.io/embed/job_board(?:/js)?\?for=([a-z0-9\-_]+)',
        page_html, re.IGNORECASE,
    )
    if m:
        result = _try_api(m.group(1))
        if result is not None:
            return result

    # Method 2: direct job URL in HTML — boards.greenhouse.io/SLUG/jobs/ID
    m = re.search(
        r'boards\.greenhouse\.io/([a-z0-9][a-z0-9\-_]{2,})/jobs/' + re.escape(job_id),
        page_html, re.IGNORECASE,
    )
    if m:
        result = _try_api(m.group(1))
        if result is not None:
            return result

    # Method 3: derive slug from domain
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    # "jobs.elastic.co" → "elastic", "oasishealth.com" → "oasishealth"
    domain_slug = re.sub(r'^(jobs\.|www\.|careers\.)', '', domain).split('.')[0]
    result = _try_api(domain_slug)
    if result is not None:
        return result

    return None  # couldn't determine via API — caller falls back to page inspection


# ── Workday CXS API path ──────────────────────────────────────────────────────

def _check_workday_api(job: dict, checked_at: str) -> dict | None:
    """
    Check a Workday job via the CXS JSON API.
    Returns an enriched job dict, or None if the URL isn't Workday / API failed.
    """
    import requests

    url = (job.get('application_url') or '').strip()
    m = _WD_URL_RE.match(url)
    if not m:
        return None

    company = m.group(1)
    wd_host = company + m.group(2)
    tenant  = m.group(3)
    job_id  = m.group(4)
    api_url = f"https://{wd_host}/wday/cxs/{company}/{tenant}/jobs"

    try:
        import requests as req
        r = req.post(
            api_url,
            json={"limit": 1, "offset": 0, "searchText": job_id},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
            timeout=12,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    if data.get('total', 0) == 0:
        return {**job, 'is_live': False, 'salary_posted': None,
                'date_posted': None, 'location_verified': None,
                'liveness_checked_at': checked_at}

    postings = data.get('jobPostings', [])
    date_p = loc_v = None

    if postings:
        p = postings[0]
        posted_on = (p.get('postedOn') or '').strip()
        dm = re.search(r'(\d+)\s+days?\s+ago', posted_on, re.IGNORECASE)
        if dm and int(dm.group(1)) <= 365:
            date_p = (date.today() - timedelta(days=int(dm.group(1)))).isoformat()
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', posted_on):
            date_p = posted_on
        loc_raw = (p.get('locationsText') or '').strip()
        if loc_raw:
            loc_v = loc_raw

    return {
        **job,
        'is_live': True,
        'salary_posted': None,
        'date_posted': date_p,
        'location_verified': loc_v,
        'liveness_checked_at': checked_at,
    }


# ── Playwright page verifier ──────────────────────────────────────────────────

async def _verify_one(page, job: dict, checked_at: str) -> dict:
    """Verify a single job posting by fully rendering its page in Playwright."""
    url      = (job.get('application_url') or '').strip()
    platform = job.get('platform_source', '')

    def _closed(**kw) -> dict:
        return {**job, 'is_live': False, 'salary_posted': None,
                'date_posted': None, 'location_verified': None,
                'liveness_checked_at': checked_at, **kw}

    def _live(**kw) -> dict:
        return {**job, 'is_live': True, 'liveness_checked_at': checked_at,
                'salary_posted': None, 'date_posted': None,
                'location_verified': None, **kw}

    if not url:
        return _closed()

    # ── iCIMS pre-flight: reject bad URLs before spending time loading them ──
    if 'icims.com' in url.lower():
        url_lower = url.lower()
        # notFound=1 in stored URL = job was already removed when chunk scraped
        if 'notfound=1' in url_lower:
            return _closed()
        # /jobs/search in URL = landed on search page, not a job detail page
        if '/jobs/search' in url_lower:
            return _closed()

    try:
        nav_resp = await page.goto(url, wait_until='domcontentloaded', timeout=30_000)

        # ── HTTP status check: Lever returns 404 for removed jobs ───────────
        if nav_resp and nav_resp.status >= 400:
            return _closed()

        # Allow JS-heavy SPAs (iCIMS, BambooHR, Ashby) to finish rendering
        try:
            await page.wait_for_load_state('networkidle', timeout=20_000)
        except PWTimeout:
            pass  # use whatever rendered

        final_url  = page.url
        page_title = (await page.title()).strip()

        # ── URL-based closed detection ──────────────────────────────────────
        if 'error=true' in final_url.lower():
            return _closed()
        if 'notfound=1' in final_url.lower():
            return _closed()
        # iCIMS redirects removed jobs to its search page
        if 'icims.com' in final_url.lower() and '/jobs/search' in final_url.lower():
            return _closed()

        # ── Generic/error page title → no job content ───────────────────────
        if page_title.lower() in _GENERIC_TITLES:
            # Give iCIMS SPAs more time — their SPA may still be booting
            if 'icims.com' in final_url.lower():
                try:
                    await page.wait_for_load_state('networkidle', timeout=15_000)
                    page_title = (await page.title()).strip()
                except PWTimeout:
                    pass
            if page_title.lower() in _GENERIC_TITLES:
                # Still generic after extra wait — treat as closed
                # (but only for non-iCIMS to avoid false positives;
                #  iCIMS continues below with body-text checks)
                if 'icims.com' not in final_url.lower():
                    return _closed()

        # ── Greenhouse embed: verify via API before relying on page text ─────
        # Company-website Greenhouse embeds (e.g. ?gh_jid=12345) return HTTP 200
        # even when the job is gone — the Greenhouse widget just shows an error
        # inside an iframe that body text can't always capture.
        if 'gh_jid=' in url.lower():
            try:
                page_html = await page.content()
            except Exception:
                page_html = ''
            gh_result = _check_greenhouse_embed(url, page_html, job, checked_at)
            if gh_result is not None:
                return gh_result
            # API couldn't determine liveness — fall through to page inspection

        # ── Collect all rendered text (main frame + child frames) ───────────
        body_text = await _all_text(page)
        body_lower = body_text.lower()

        # ── Closed-phrase detection ─────────────────────────────────────────
        for phrase in _CLOSED_PHRASES:
            if phrase in body_lower:
                return _closed()

        # ── Redirect depth check (walked back to careers home) ──────────────
        def _depth(u: str) -> int:
            return len([p for p in u.split('/') if p and 'http' not in p])
        if url != final_url and (_depth(url) - _depth(final_url)) >= 2 and _depth(final_url) <= 2:
            return _closed()

        # ── JSON-LD extraction (main frame first, then child frames) ────────
        extras: dict = {}
        jsonld = await _get_jsonld(page)
        if not jsonld:
            for frame in page.frames:
                if frame != page.main_frame:
                    try:
                        jsonld = await _get_jsonld(frame)
                        if jsonld:
                            break
                    except Exception:
                        pass

        if jsonld:
            parsed = _parse_jsonld(jsonld)
            if parsed.get('expired'):
                return _closed()
            for k in ('date_posted', 'salary_posted', 'location_verified'):
                if parsed.get(k):
                    extras[k] = parsed[k]

        # ── Salary fallback: scan rendered text ─────────────────────────────
        if not extras.get('salary_posted'):
            sal = _extract_salary(body_text)
            if sal:
                extras['salary_posted'] = sal

        # ── Date fallback: scan rendered text ────────────────────────────────
        if not extras.get('date_posted'):
            dt = _extract_date(body_text)
            if dt:
                extras['date_posted'] = dt

        # ── iCIMS: always verify location (chunk data is unreliable) ────────
        # This applies to ALL iCIMS jobs regardless of what the chunk stored.
        # The chunk's remote flag and location codes are both unreliable.
        if platform == 'iCIMS':
            loc = extras.get('location_verified') or \
                  await _extract_icims_location(page, body_text, page_title)

            if loc:
                extras['location_verified'] = loc
                if _location_is_excluded(loc):
                    return _closed()
            else:
                # Page rendered successfully but location is undetectable.
                # We exclude rather than show a job with unknown location —
                # the user explicitly wants no wrong-location results.
                log.debug("iCIMS location unverifiable for %s — excluding", url)
                return _closed()

        # ── Non-iCIMS: apply location filter to extracted location ──────────
        elif extras.get('location_verified'):
            if _location_is_excluded(extras['location_verified']):
                return _closed()

        return _live(**extras)

    except PWTimeout:
        log.warning("Playwright timeout: %s", url)
        # Timeout = technical failure, not a job removal signal.
        # Keep the job but leave location_verified=None (frontend shows ⚠).
        return _live()
    except Exception as exc:
        log.warning("Playwright error for %s: %s", url, exc)
        return _live()


# ── Main async runner ─────────────────────────────────────────────────────────

async def _run_all(jobs: list[dict], max_concurrent: int) -> list[dict]:
    """Dispatch all jobs: Workday via CXS API, everything else via Playwright."""
    results: list[dict] = []
    sem = asyncio.Semaphore(max_concurrent)
    checked_at = _now_iso()
    total = len(jobs)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-extensions',
                '--disable-background-networking',
            ],
        )

        async def _dispatch(job: dict) -> dict:
            url = (job.get('application_url') or '').strip()

            # Workday: use fast CXS API — no browser needed
            if _WD_URL_RE.match(url):
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, _check_workday_api, job, checked_at
                )
                if result is not None:
                    return result
                # API failed — fall through to Playwright

            # All other platforms (and Workday API failures): headless browser
            async with sem:
                ctx = await browser.new_context(
                    user_agent=(
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/124.0.0.0 Safari/537.36'
                    ),
                    java_script_enabled=True,
                    ignore_https_errors=True,
                )
                page = await ctx.new_page()
                try:
                    return await _verify_one(page, job, checked_at)
                except Exception as exc:
                    log.warning("Unexpected error for %s: %s", url, exc)
                    return {**job, 'is_live': True, 'salary_posted': None,
                            'date_posted': None, 'location_verified': None,
                            'liveness_checked_at': checked_at}
                finally:
                    await ctx.close()

        tasks = [_dispatch(job) for job in jobs]
        done  = 0
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
            done += 1
            if done % 10 == 0 or done == total:
                live = sum(1 for r in results if r.get('is_live', True))
                print(f"  [{done}/{total}] verified — {live} live so far …", flush=True)

        await browser.close()

    return results


# ── Public entry point ────────────────────────────────────────────────────────

def verify_all_jobs(jobs: list[dict], max_concurrent: int = 5) -> list[dict]:
    """
    Verify each job by rendering its posting page in headless Chromium.
    Workday jobs use the CXS JSON API (fast + reliable); all others use Playwright.
    Blocks until all jobs are processed. Returns the enriched job list.
    """
    if not jobs:
        return jobs
    return asyncio.run(_run_all(jobs, max_concurrent))
