#!/usr/bin/env python3
"""
Job agent: checks company career boards for Jerusalem jobs and sends a daily
Telegram summary.

Rules:
- Every field in the message (title, company, location, date, link) is copied
  from the source data. Nothing is generated or guessed.
- A link is included only if it was fetched successfully (HTTP 200) in this run.
- If a source fails, the message says so instead of filling in the gap.

Usage:
  python job_agent.py            # normal run (respects the 19:00 Israel-time gate)
  python job_agent.py --force    # run now, ignore the time gate
  python job_agent.py --test     # only send a test message to Telegram
  python job_agent.py --dry      # run everything but print instead of sending

Environment variables (use GitHub Secrets, never put them in code):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import os
import re
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import requests

IL = ZoneInfo("Asia/Jerusalem")
MAX_AGE_HOURS = 72
MAX_YEARS_ALLOWED = 3  # exclude if the posting mentions more than this many years

# ---------------------------------------------------------------------------
# Sources. Add companies here once their career-board system is verified.
# type "lever": public Lever postings API. Mobileye uses the EU instance.
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "company": "Mobileye",
        "type": "lever",
        "api": "https://api.eu.lever.co/v0/postings/mobileye?mode=json",
        "careers_page": "https://careers.mobileye.com",
    },
    {
        # Verified by probe.py: Greenhouse board "triplewhale" has Jerusalem jobs
        # and every one carries first_published.
        "company": "Triple Whale",
        "type": "greenhouse",
        "api": "https://boards-api.greenhouse.io/v1/boards/triplewhale/jobs?content=true",
        "careers_page": "https://job-boards.greenhouse.io/triplewhale",
    },
    {
        # careers.lightricks.com loads its jobs from Greenhouse board "lightricks"
        # (seen in the page's own script). The board had 0 open jobs on 2026-11-24.
        "company": "Lightricks",
        "type": "greenhouse",
        "api": "https://boards-api.greenhouse.io/v1/boards/lightricks/jobs?content=true",
        "careers_page": "https://careers.lightricks.com/careers",
    },
]

# The title must name a software-development role in the target stack...
INCLUDE_TITLE = re.compile(
    r"(full[\s-]?stack|front[\s-]?end|back[\s-]?end|software|developer|web|"
    r"\.net|c#|angular|react|node|python|java\b|typescript)",
    re.IGNORECASE,
)
# ...and must not name a level or a domain outside the target.
EXCLUDE_TITLE = re.compile(
    r"\b(senior|sr\.?|lead\w*|principal|staff|head|director|vp|manager|architect|"
    r"intern|student|junior|jr\.?|"
    r"embedded|firmware|hardware|asic|verification|validation|algorithm\w*|c\+\+)",
    re.IGNORECASE,
)
YEARS_RE = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?years?", re.IGNORECASE)

# Staffing agencies are not in SOURCES at all, so they can never appear.
# This list is an extra safety net on the employer name returned by a source.
STAFFING_BLACKLIST = ["top-soft", "tops-soft", "manpower", "matrix", "ness"]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_lever(source):
    r = requests.get(source["api"], timeout=30)
    r.raise_for_status()
    jobs = []
    for p in r.json():
        cats = p.get("categories") or {}
        created_ms = p.get("createdAt")
        if not created_ms:
            continue  # no publish date -> never included
        jobs.append(
            {
                "company": source["company"],
                "title": p.get("text", "").strip(),
                "location": (cats.get("location") or "").strip(),
                "all_locations": [x for x in (cats.get("allLocations") or [])],
                "commitment": (cats.get("commitment") or "").strip(),
                "workplace": (p.get("workplaceType") or "").strip(),
                "created": dt.datetime.fromtimestamp(created_ms / 1000, tz=dt.timezone.utc),
                "link": p.get("hostedUrl", ""),
                "description": p.get("descriptionPlain", "") or "",
            }
        )
    return jobs


def fetch_greenhouse(source):
    r = requests.get(source["api"], timeout=30)
    r.raise_for_status()
    jobs = []
    for p in r.json().get("jobs", []):
        published = p.get("first_published")
        if not published:
            continue  # no publish date -> never included
        text = html.unescape(p.get("content") or "")
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        jobs.append(
            {
                "company": source["company"],
                "title": (p.get("title") or "").strip(),
                "location": ((p.get("location") or {}).get("name") or "").strip(),
                "all_locations": [o.get("name") or "" for o in (p.get("offices") or [])],
                # Greenhouse's public API has no employment-type field.
                "commitment": None,
                "workplace": "",
                "created": dt.datetime.fromisoformat(published).astimezone(dt.timezone.utc),
                "link": p.get("absolute_url", ""),
                "description": " ".join(text.split()),
            }
        )
    return jobs


FETCHERS = {"lever": fetch_lever, "greenhouse": fetch_greenhouse}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def is_recent(job, now):
    return (now - job["created"]) <= dt.timedelta(hours=MAX_AGE_HOURS)


def in_jerusalem(job):
    places = [job["location"]] + job["all_locations"]
    return any("jerusalem" in p.lower() for p in places)


def is_full_time(job):
    # None = the source has no employment-type field; the job is kept and the
    # message says the type was not stated, rather than guessing it.
    if job["commitment"] is None:
        return True
    return "full" in job["commitment"].lower()


def level_ok(job):
    if not INCLUDE_TITLE.search(job["title"]) or EXCLUDE_TITLE.search(job["title"]):
        return False
    years = [int(m.group(1)) for m in YEARS_RE.finditer(job["description"])]
    if years and max(years) > MAX_YEARS_ALLOWED:
        return False
    return True


def years_note(job):
    """Return text copied from the posting about years of experience, or None."""
    m = YEARS_RE.search(job["description"])
    if not m:
        return None
    start = max(0, m.start() - 40)
    end = min(len(job["description"]), m.end() + 40)
    return " ".join(job["description"][start:end].split())


def not_staffing(job):
    name = job["company"].lower()
    return not any(b in name for b in STAFFING_BLACKLIST)


def link_is_live(url):
    """A job counts as open only if its page loads in this run."""
    if not url:
        return False
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------
def build_message(jobs, checked, failed, now):
    date_str = now.astimezone(IL).strftime("%d.%m.%Y %H:%M")
    lines = [f"עדכון משרות, {date_str}", ""]
    if jobs:
        lines.append(f"נמצאו {len(jobs)} משרות חדשות ב-72 השעות האחרונות:")
        for j in jobs:
            created = j["created"].astimezone(IL).strftime("%d.%m.%Y")
            lines.append("")
            lines.append(f"{j['title']}")
            lines.append(f"חברה: {j['company']}")
            lines.append(f"מיקום: {j['location']}")
            lines.append(f"סוג משרה: {j['commitment'] or 'לא צוין במקור'}")
            lines.append(f"דרך עבודה לפי המקור: {j['workplace'] or 'לא צוין'}")
            lines.append(f"פורסם: {created}")
            note = years_note(j)
            lines.append(f"ניסיון לפי הטקסט במקור: {note if note else 'לא צוין'}")
            lines.append(j["link"])
    else:
        lines.append("לא נמצאו משרות חדשות ב-72 השעות האחרונות.")
    lines.append("")
    lines.append("חברות שנבדקו: " + (", ".join(checked) if checked else "אין"))
    if failed:
        lines.append("לא ניתן היה לבדוק: " + ", ".join(failed))
    return "\n".join(lines)


def split_message(text, limit=4000):
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur:
        parts.append(cur)
    return parts


def send_telegram(text, dry=False):
    if dry:
        print(text)
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    for part in split_message(text):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": part, "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            print(f"Telegram error: {r.status_code} {r.text}", file=sys.stderr)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = set(sys.argv[1:])
    dry = "--dry" in args

    if "--test" in args:
        send_telegram("הודעת בדיקה מסוכן המשרות. החיבור לטלגרם עובד.", dry)
        return

    now = dt.datetime.now(dt.timezone.utc)
    manual = "--force" in args or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not manual and now.astimezone(IL).hour != 19:
        print("Not 19:xx Israel time, skipping.")
        return

    found, checked, failed = [], [], []
    for src in SOURCES:
        try:
            jobs = FETCHERS[src["type"]](src)
        except Exception as e:  # noqa: BLE001
            print(f"{src['company']} failed: {e}", file=sys.stderr)
            failed.append(src["company"])
            continue
        checked.append(src["company"])
        for j in jobs:
            if not (
                is_recent(j, now)
                and in_jerusalem(j)
                and is_full_time(j)
                and level_ok(j)
                and not_staffing(j)
            ):
                continue
            if not link_is_live(j["link"]):
                failed.append(f"{src['company']}: הקישור למשרה '{j['title']}' לא נטען")
                continue
            found.append(j)

    # Remove duplicates by link
    seen, unique = set(), []
    for j in found:
        if j["link"] not in seen:
            seen.add(j["link"])
            unique.append(j)

    unique.sort(key=lambda j: j["created"], reverse=True)
    send_telegram(build_message(unique, checked, failed, now), dry)


if __name__ == "__main__":
    main()
