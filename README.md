# Job Board Aggregator — IT Leadership Alert System

A two-part tool in one repo:

| Layer | What it does |
|---|---|
| **Generic aggregator** (`scripts/scraper.py`) | Scrapes 1M+ jobs from all ATS platforms for the public job board UI |
| **Personal alert system** (`alerts/`) | Monitors ~80 curated companies for Director/VP/Head-of-IT roles, deduplicates them, and publishes an RSS feed you can subscribe to in Feedly or Inoreader |

The rest of this README covers the **personal alert system** in `alerts/`.

---

## How It Works

```
companies.yaml
     │
     ▼
scrapers.py ──► Greenhouse API ──┐
              ► Lever API        ├──► filter_jobs() ──► dedup.py ──► feed_generator.py
              ► Workday CXS API ─┘         │                              │
                                           ▼                              ▼
                                   (keyword + location             job_feed.xml  ← RSS feed
                                    + exclusion filter)            jobs_db.json  ← full DB
                                                                   seen_jobs.json ← dedup state
```

On each run:
1. Fetches jobs from every company in `companies.yaml`
2. Filters for your target titles and locations
3. Compares against `seen_jobs.json` to find **new** listings
4. Writes new listings to `job_feed.xml` (your RSS feed) and all open listings to `jobs_db.json`
5. GitHub Actions commits the updated files; GitHub Pages serves them

---

## How It Searches 21,000+ Companies

The alert system doesn't scrape companies directly. It reads the pre-built job chunks already produced by `scripts/scraper.py` (the generic aggregator that runs daily):

```
data/chunks/jobs_chunk_0.json.gz   ← 25,000 jobs
data/chunks/jobs_chunk_1.json.gz   ← 25,000 jobs
...
data/chunks/jobs_chunk_56.json.gz
```

These chunks cover **1.4 million jobs from 21,000+ companies** across all ATS platforms. The alert system reads all of them in ~30 seconds (zero API calls) and applies your keyword + location + exclusion filters. Coverage automatically grows as new companies are added to the generic scraper's company lists.

Use `--mode live` if you need same-day freshness for the 83 companies in `companies.yaml`:

```bash
python main.py --mode live   # scrapes 83 companies directly, ~90 seconds
```

---

## Quick Start (local)

### 1. Install dependencies

```bash
pip install requests pyyaml
```

### 2. Run the alert system

```bash
# From the repo root — reads pre-built chunks (1.4M jobs, 21k+ companies)
cd alerts
python main.py
```

Or with a dry run (reads and filters but writes nothing):

```bash
cd alerts
python main.py --dry-run
```

### 3. View output

| File | Purpose |
|---|---|
| `job_feed.xml` | RSS 2.0 feed — subscribe in Feedly/Inoreader |
| `jobs_db.json` | Full structured backup of all open matching jobs |
| `seen_jobs.json` | Dedup state — tracks which job IDs you've already seen |
| `alerts.html` | GitHub Pages UI showing the latest matches |

---

## Configuration

### `alerts/config.py` — keywords, locations, exclusions

```python
# Add new target title phrases here (substring match, case-insensitive):
TARGET_KEYWORDS = [
    "director of it",
    "vp infrastructure",
    "head of it",
    ...
]

# Add strings that should auto-discard a job:
EXCLUSION_TERMS = [
    "software engineer",
    "intern",
    ...
]

# Locations — job must contain at least one of these:
ALLOWED_LOCATIONS = [
    "remote",
    "jacksonville",
    ", fl",
    ...
]

# Keep jobs whose location field is blank (e.g. some Workday boards):
ALLOW_UNSPECIFIED_LOCATION = False

# How many days before a closed job is pruned from seen_jobs.json:
PRUNE_AFTER_DAYS = 30
```

Also update `FEED_LINK` and `FEED_SELF_LINK` to your actual GitHub Pages URL:

```python
FEED_LINK = "https://<your-github-username>.github.io/job-board-aggregator-main/alerts.html"
FEED_SELF_LINK = "https://<your-github-username>.github.io/job-board-aggregator-main/job_feed.xml"
```

---

### `alerts/companies.yaml` — adding and removing companies

This is the **single place** to manage which companies the scraper monitors.
The scraper reads this file on every run — no code changes needed.

#### Add a Greenhouse company

Find the slug at `https://boards.greenhouse.io/<slug>`:

```yaml
greenhouse:
  - slug: "servicenow"
    name: "ServiceNow"
    tags: [itsm, enterprise]
```

#### Add a Lever company

Find the slug at `https://jobs.lever.co/<slug>`:

```yaml
lever:
  - slug: "cloudflare"
    name: "Cloudflare"
    tags: [cloud, networking]
```

#### Add a Workday company

Format: `"company|wd<number>|site_id"` — find these parts in the Workday URL:
`https://<company>.wd<N>.myworkdayjobs.com/en-US/<site_id>`:

```yaml
workday:
  - slug: "cisco|wd5|cisco_careers"
    name: "Cisco"
    tags: [networking]
```

#### Remove a company

Delete its entry from `companies.yaml`. The scraper will stop querying it
immediately.  If the company's jobs are already in `seen_jobs.json`, they will
age out naturally after `PRUNE_AFTER_DAYS` days.

#### Validate your company list

Run the discovery module to check which slugs are currently returning valid
job boards:

```bash
cd alerts
python discovery.py --validate
```

To probe a file of new candidate slugs before adding them:

```bash
# candidates.txt format — one per line:
# greenhouse:some-company
# lever:some-company
# workday:company|wd5|site_id

python discovery.py --probe candidates.txt
```

---

## GitHub Actions — Automated Scheduling

The workflow at `.github/workflows/job-alerts.yml` runs every 6 hours
(00:00, 06:00, 12:00, 18:00 UTC) and also has a manual trigger button.

On each run it:
1. Installs `requests` and `pyyaml`
2. Runs `python alerts/main.py`
3. Commits updated `job_feed.xml`, `jobs_db.json`, and `seen_jobs.json` to the repo
4. Posts a step summary showing how many new jobs were found

### Enable GitHub Actions

The alert workflow triggers automatically whenever the **"Update Job Listings"** workflow (the daily scraper) finishes — so your alerts are always filtered against freshly-scraped data. It also has a daily fallback at 14:00 UTC and a manual trigger button.

To trigger manually: **Actions → Job Alert System → Run workflow**.

### Enable GitHub Pages

1. Go to **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: **main** | Folder: **/ (root)**
4. Click **Save**

After a minute your RSS feed and alerts page will be live at:
```
https://<your-username>.github.io/job-board-aggregator-main/job_feed.xml
https://<your-username>.github.io/job-board-aggregator-main/alerts.html
```

---

## Deduplication and Pruning

### How dedup works

`seen_jobs.json` stores every `job_id` that has ever passed your filters,
along with the dates it was first and last seen:

```json
{
  "a3f1b2c4d5e6f789": {
    "first_seen": "2024-06-01",
    "last_seen":  "2024-06-15"
  }
}
```

On each run:
- Jobs **not** in `seen_jobs.json` → **NEW** (appear in RSS feed, added to seen)
- Jobs **already** in `seen_jobs.json` → existing (kept in `jobs_db.json`, not re-surfaced in RSS)

The RSS feed (`job_feed.xml`) therefore only contains the **net-new** listings
from the most recent run, which is what makes it useful as an alert feed.

### How pruning works

A job is pruned from `seen_jobs.json` and stops appearing in `jobs_db.json`
when **both** of these are true:
1. It no longer appears in any ATS board (the posting was closed/removed)
2. Its `last_seen` date is older than `PRUNE_AFTER_DAYS` (default: 30)

This prevents `seen_jobs.json` from growing unboundedly while still keeping a
grace window for postings that temporarily disappear and reappear.

### Reset seen_jobs.json

If you want to re-surface all currently-open matching jobs as "new" (e.g.,
after significantly changing your filters), delete the seen file:

```bash
cd alerts
python main.py --reset-seen

# Then run normally — all open matches will appear as new in the next RSS update:
python main.py
```

Or delete the file manually: `rm seen_jobs.json`

---

## File Reference

```
job-board-aggregator-main/
├── alerts/
│   ├── config.py          ← All tunable settings (edit this)
│   ├── companies.yaml     ← Company list (edit this)
│   ├── scrapers.py        ← Greenhouse / Lever / Workday fetch + filter logic
│   ├── discovery.py       ← Validate or probe company slugs (run standalone)
│   ├── dedup.py           ← seen_jobs.json read/write/prune
│   ├── feed_generator.py  ← RSS 2.0 + jobs_db.json serialization
│   └── main.py            ← Entry point (run this)
├── .github/workflows/
│   ├── scrape-jobs.yml    ← Generic aggregator (existing, unchanged)
│   └── job-alerts.yml     ← Personal alert system (new)
├── alerts.html            ← GitHub Pages UI for your job matches
├── job_feed.xml           ← RSS 2.0 feed (generated)
├── jobs_db.json           ← Full job database (generated)
└── seen_jobs.json         ← Dedup state (generated, committed)
```
