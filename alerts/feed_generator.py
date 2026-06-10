"""
RSS 2.0 feed generator and jobs_db.json serializer.

RSS feed  → job_feed.xml   (contains only NEW jobs from the latest run)
Job DB    → jobs_db.json   (all currently-open matching jobs, with metadata)
"""

import json
import logging
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

from config import (
    FEED_DESCRIPTION,
    FEED_LINK,
    FEED_SELF_LINK,
    FEED_TITLE,
    JOB_FEED_FILE,
    JOBS_DB_FILE,
)

log = logging.getLogger(__name__)


# ─────────────────────────── jobs_db.json ───────────────────────────────────


def save_jobs_db(jobs: list[dict]) -> None:
    """
    Write all currently-open matching jobs to jobs_db.json.

    Jobs are sorted newest-first by first_seen, then by company name.
    """
    sorted_jobs = sorted(
        jobs,
        key=lambda j: (j.get("first_seen", "") or "", j.get("company", "").lower()),
        reverse=True,
    )

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total": len(sorted_jobs),
        "jobs": sorted_jobs,
    }

    tmp = JOBS_DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, JOBS_DB_FILE)
    log.info("Saved %d jobs to jobs_db.json", len(sorted_jobs))


# ─────────────────────────── RSS helpers ────────────────────────────────────


def _rfc822(dt: datetime) -> str:
    """Format a datetime as RFC 822 (required by RSS 2.0)."""
    return format_datetime(dt)


def _pub_date(job: dict) -> datetime:
    """
    Best-effort posted date for a job, falling back to now().

    Greenhouse returns ISO-8601 updated_at; Lever returns epoch ms;
    Workday doesn't expose posted date — we use first_seen.
    """
    for field in ("date_posted", "first_seen"):
        raw = (job.get(field) or "").strip()
        if raw:
            try:
                return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return datetime.now(timezone.utc)


def _build_item(job: dict) -> str:
    """Return an RSS <item> XML block for one job."""
    title = escape(f"{job.get('title', 'Untitled')} — {job.get('company', '')}")
    link = escape(job.get("application_url", ""))
    location = escape(job.get("location", "Not specified") or "Not specified")
    platform = escape(job.get("platform_source", ""))
    first_seen = escape(job.get("first_seen", ""))
    pub_date = _rfc822(_pub_date(job))
    guid = escape(job.get("job_id", link))
    desc = escape(
        f"{job.get('title', '')} at {job.get('company', '')} "
        f"({location}) via {platform}. First seen: {first_seen}."
    )

    return f"""    <item>
      <title>{title}</title>
      <link>{link}</link>
      <description>{desc}</description>
      <pubDate>{pub_date}</pubDate>
      <guid isPermaLink="false">{guid}</guid>
      <category>{platform}</category>
      <source url="{FEED_SELF_LINK}">{escape(FEED_TITLE)}</source>
    </item>"""


# ─────────────────────────── RSS feed ───────────────────────────────────────


def generate_rss_feed(new_jobs: list[dict]) -> None:
    """
    Write job_feed.xml containing only new jobs from the latest run.

    If there are no new jobs, writes a feed with a single informational item
    so subscribers know the feed is alive.
    """
    now = datetime.now(timezone.utc)
    build_date = _rfc822(now)

    items_xml = ""
    if new_jobs:
        # Sort newest-first by pub_date
        ordered = sorted(new_jobs, key=_pub_date, reverse=True)
        items_xml = "\n".join(_build_item(j) for j in ordered)
    else:
        items_xml = f"""    <item>
      <title>No new matches this run ({now.strftime('%Y-%m-%d %H:%M UTC')})</title>
      <link>{escape(FEED_LINK)}</link>
      <description>The alert system ran but found no new listings matching your filters.</description>
      <pubDate>{build_date}</pubDate>
      <guid isPermaLink="false">no-new-{now.strftime('%Y%m%d%H%M')}</guid>
    </item>"""

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <link>{escape(FEED_LINK)}</link>
    <description>{escape(FEED_DESCRIPTION)}</description>
    <language>en-us</language>
    <lastBuildDate>{build_date}</lastBuildDate>
    <ttl>360</ttl>
    <atom:link href="{escape(FEED_SELF_LINK)}" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>
"""

    tmp = JOB_FEED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(feed_xml)
    os.replace(tmp, JOB_FEED_FILE)
    log.info(
        "Wrote job_feed.xml with %d new item(s)",
        len(new_jobs) if new_jobs else 0,
    )
