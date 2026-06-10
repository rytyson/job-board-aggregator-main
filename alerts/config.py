"""
Central configuration for Ryan's IT-leadership job alert system.
Edit this file to tune keywords, locations, companies path, and pruning behavior.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)

# ---------- File paths ----------
COMPANIES_FILE = os.path.join(BASE_DIR, "companies.yaml")
SEEN_JOBS_FILE = os.path.join(REPO_ROOT, "seen_jobs.json")
JOBS_DB_FILE = os.path.join(REPO_ROOT, "jobs_db.json")
JOB_FEED_FILE = os.path.join(REPO_ROOT, "job_feed.xml")

# Chunks directory — produced by scripts/scraper.py (runs daily via scrape-jobs.yml).
# Contains 1.4M+ jobs from 21,000+ companies across all ATS platforms.
CHUNKS_DIR = os.path.join(REPO_ROOT, "data", "chunks")
CHUNKS_MANIFEST = os.path.join(CHUNKS_DIR, "jobs_manifest.json")

# ---------- RSS feed metadata ----------
FEED_TITLE = "Ryan's IT Leadership Job Alerts"
FEED_DESCRIPTION = (
    "Director / VP / Head-of-IT roles in Operations, Infrastructure, "
    "Service Delivery, and Managed Services — Remote or Jacksonville, FL"
)
# Update these to your actual GitHub Pages URL once the repo is published.
FEED_LINK = "https://rytyson.github.io/job-board-aggregator-main/alerts.html"
FEED_SELF_LINK = "https://rytyson.github.io/job-board-aggregator-main/job_feed.xml"

# ---------- Location filter ----------
# A job PASSES if its normalized location contains ANY of these substrings
# (case-insensitive).  Order doesn't matter.
ALLOWED_LOCATIONS = [
    "remote",
    "jacksonville",
    " jax",
    ", fl",
    ", florida",
    " fl,",
    " florida",
    "anywhere",
    "work from home",
    "wfh",
    # "worldwide" and "global" intentionally omitted — they appear in city/site names
    # ("Bonifacio Global City", "Global View") and cause false positives.
]

# Bare country/region strings that count as a remote/country-wide role.
# These are checked via EXACT MATCH (after stripping the location) so that
# "United States" matches but "Spring, Texas, United States of America" does not.
ALLOWED_BARE_LOCATIONS = {
    "united states",
    "united states of america",
    "usa",
    "u.s.",
    "u.s.a.",
}

# Non-US country indicators — if ANY of these appear in the location alongside
# "remote", the job is excluded.  This prevents e.g. "China-Remote Location-Beijing"
# from matching just because it contains the word "remote".
EXCLUDED_COUNTRIES = [
    "china", "india", "germany", "france", "spain", "italy", "brazil",
    "canada", "australia", "uk", "united kingdom", "england", "netherlands",
    "sweden", "norway", "denmark", "singapore", "japan", "korea", "israel",
    "ireland", "poland", "mexico", "argentina", "colombia", "philippines",
    "portugal", "switzerland", "austria", "belgium", "czech", "hungary",
    "romania", "south africa", "nigeria", "kenya", "egypt", "uae",
    # ISO country codes used by some ATS systems (Workday especially)
    "- phl", "- ind", "- chn", "- gbr", "- deu", "- fra", "- aus",
    "- bra", "- mex", "- sgp", "- jpn", "- kor", "- isr", "- irl",
]

# If True, jobs whose location is blank / "Not specified" are also kept.
# Set to False to only surface jobs with an explicitly matching location.
ALLOW_UNSPECIFIED_LOCATION = False

# ---------- Target title keywords ----------
# A job PASSES the keyword filter if its title (lowercased) CONTAINS any of
# these strings.  Add new phrases here — no other code changes required.
TARGET_KEYWORDS = [
    # Director-level IT
    # Note: "director, it" uses a trailing space to avoid matching "Director, Italy"
    "director of it ",
    "director of it,",
    "director, it ",
    "director, it,",
    "it director",
    "director of information technology",
    # Director-level Infrastructure
    "director of infrastructure",
    "infrastructure director",
    "director of technology infrastructure",
    # Director-level Service Delivery
    "director of service delivery",
    "service delivery director",
    # Director-level Managed Services
    "director of managed services",
    "managed services director",
    # Director-level Operations — kept specific to avoid non-IT false positives
    "director of enterprise operations",
    "director of it operations",
    "director of technology operations",
    # Note: bare "director of operations" removed — too many food/service/nonprofit hits
    # Senior Manager variants
    "senior manager it",
    "senior manager of it",
    "senior manager, it",
    "senior manager infrastructure",
    "senior manager of infrastructure",
    "manager of infrastructure operations",
    "infrastructure operations manager",
    # VP / Vice President
    "vp it",
    "vp of it",
    "vp infrastructure",
    "vp of infrastructure",
    "vice president it",
    "vice president, it",
    "vice president infrastructure",
    "vice president of it",
    "vice president of infrastructure",
    # Head of
    "head of it",
    "head of information technology",
    "head of infrastructure",
    "head of technology",
    # Incident & Implementation
    "major incident manager",
    "technical implementation manager",
    "it implementation manager",
]

# ---------- Exclusion terms ----------
# A job is DISCARDED if its title (lowercased) contains ANY of these strings.
# These take precedence — an exclusion match always wins.
EXCLUSION_TERMS = [
    "software engineer",
    "software developer",
    "software development",
    "frontend",
    "front-end",
    "front end",
    "backend",
    "back-end",
    "back end",
    "full stack",
    "fullstack",
    "full-stack",
    "data scientist",
    "data engineer",
    "data analyst",
    "machine learning",
    "ml engineer",
    "devops engineer",
    "site reliability engineer",
    " sre",
    "product manager",
    "program manager",
    "project manager",
    "intern",
    "internship",
    "entry level",
    "entry-level",
    "junior ",
    " jr ",
    "associate engineer",
    "staff engineer",
    "principal engineer",
    "alliances",        # "Head of Technology Alliances" = BD, not IT ops
    "partnerships",     # "Head of Technology Partnerships" = BD, not IT ops
]

# ---------- Dedup / pruning ----------
# Jobs absent from all ATS sources for this many consecutive days are pruned
# from seen_jobs.json and jobs_db.json.
PRUNE_AFTER_DAYS = 30

# ---------- HTTP request throttling ----------
REQUEST_DELAY_MIN = 0.3   # seconds between requests (per worker thread)
REQUEST_DELAY_MAX = 1.2

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ---------- Workday search terms ----------
# Sent as `searchText` in Workday's CXS POST API to pre-filter large boards.
# Results from all terms are merged and de-duplicated before local filtering.
WORKDAY_SEARCH_TERMS = [
    "director",
    "VP infrastructure",
    "head of IT",
    "incident manager",
]
# Hard cap on results per (company, search_term) to keep run times bounded.
WORKDAY_MAX_RESULTS_PER_SEARCH = 100

# ---------- Concurrency ----------
MAX_WORKERS_GREENHOUSE = 20
MAX_WORKERS_LEVER = 20
MAX_WORKERS_WORKDAY = 5   # Workday rate-limits aggressively; keep this low
