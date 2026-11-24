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
import json
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
    # Found on the Made in JLM Jerusalem jobs board (ei.fo/board/madeinjlm) and
    # verified by probe.py on 2026-11-24.
    {
        "company": "IDT",
        "type": "lever",
        "api": "https://api.lever.co/v0/postings/idt?mode=json",
        "careers_page": "https://jobs.lever.co/idt",
    },
    {
        "company": "Wellthy",
        "type": "greenhouse",
        "api": "https://boards-api.greenhouse.io/v1/boards/wellthy/jobs?content=true",
        "careers_page": "https://wellthy.com/careers",
    },
    {
        # Comeet gives no publish date (only time_updated), so these jobs are
        # reported by the date this agent first saw them, in a separate section.
        "company": "Cross River",
        "type": "comeet",
        "page": "https://www.comeet.com/jobs/crossriver/C7.00F",
        "careers_page": "https://www.comeet.com/jobs/crossriver/C7.00F",
    },
]

# Where first-seen dates are kept between runs (committed back by the workflow).
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

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


def fetch_comeet(source):
    r = requests.get(source["page"], timeout=30, headers={"User-Agent": "Mozilla/5.0 (job-agent)"})
    r.raise_for_status()
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);\s*\n", r.text, re.S)
    if not m:
        raise ValueError("positions data not found on the Comeet page")
    jobs = []
    for p in json.loads(m.group(1)):
        loc = p.get("location") or {}
        jobs.append(
            {
                "company": source["company"],
                "title": (p.get("name") or "").strip(),
                "location": (loc.get("name") or "").strip(),
                "all_locations": [loc.get("city") or ""],
                "commitment": p.get("employment_type"),
                "workplace": (p.get("workplace_type") or "").strip(),
                "level": p.get("experience_level"),
                "created": None,  # Comeet has no publish date
                "link": p.get("url_comeet_hosted_page") or p.get("url_active_page") or "",
                "description": "",
            }
        )
    return jobs


FETCHERS = {"lever": fetch_lever, "greenhouse": fetch_greenhouse, "comeet": fetch_comeet}

# Experience levels (as the source names them) that are above the target.
EXCLUDE_LEVELS = {"senior", "management", "executive", "director"}


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
    if (job.get("level") or "").lower() in EXCLUDE_LEVELS:
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
# First-seen state (for sources without a publish date)
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"baselined": [], "first_seen": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------
def job_lines(j, date_label, date_value):
    note = years_note(j)
    return [
        "",
        f"{j['title']}",
        f"חברה: {j['company']}",
        f"מיקום: {j['location']}",
        f"סוג משרה: {j['commitment'] or 'לא צוין במקור'}",
        f"דרך עבודה לפי המקור: {j['workplace'] or 'לא צוין'}",
        f"{date_label}: {date_value}",
        f"ניסיון לפי הטקסט במקור: {note if note else 'לא צוין'}",
        j["link"],
    ]


def build_message(dated, undated, checked, failed, baselined_now, now):
    date_str = now.astimezone(IL).strftime("%d.%m.%Y %H:%M")
    lines = [f"עדכון משרות, {date_str}", ""]
    if dated:
        lines.append(f"נמצאו {len(dated)} משרות שפורסמו ב-72 השעות האחרונות:")
        for j in dated:
            lines += job_lines(j, "פורסם", j["created"].astimezone(IL).strftime("%d.%m.%Y"))
    else:
        lines.append("לא נמצאו משרות שפורסמו ב-72 השעות האחרונות.")
    if undated:
        lines.append("")
        lines.append(
            f"עוד {len(undated)} משרות חדשות מאז הבדיקה הקודמת. "
            "במקור שלהן אין תאריך פרסום, אז התאריך הוא היום שבו הסוכן ראה אותן לראשונה:"
        )
        for j in undated:
            lines += job_lines(j, "נראתה לראשונה", j["first_seen"].astimezone(IL).strftime("%d.%m.%Y"))
    lines.append("")
    lines.append("חברות שנבדקו: " + (", ".join(checked) if checked else "אין"))
    if baselined_now:
        lines.append(
            "התחלתי לעקוב היום אחרי: " + ", ".join(baselined_now)
            + ". המשרות שכבר פתוחות שם לא נחשבות חדשות; משרות שייפתחו מעכשיו ידווחו."
        )
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
def passes_filters(j):
    return in_jerusalem(j) and is_full_time(j) and level_ok(j) and not_staffing(j)


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

    state = load_state()
    dated, undated, checked, failed, baselined_now = [], [], [], [], []
    for src in SOURCES:
        try:
            jobs = FETCHERS[src["type"]](src)
        except Exception as e:  # noqa: BLE001
            print(f"{src['company']} failed: {e}", file=sys.stderr)
            failed.append(src["company"])
            continue
        checked.append(src["company"])

        if src["type"] == "comeet":
            first_run = src["company"] not in state["baselined"]
            for j in jobs:
                key = j["link"]
                if not key:
                    continue
                if key not in state["first_seen"]:
                    # On the first run for a company, every open job is recorded with
                    # the marker "baseline" so it is never reported as new.
                    state["first_seen"][key] = "baseline" if first_run else now.isoformat()
                seen = state["first_seen"][key]
                if seen == "baseline":
                    continue
                j["first_seen"] = dt.datetime.fromisoformat(seen)
                if now - j["first_seen"] > dt.timedelta(hours=MAX_AGE_HOURS):
                    continue
                if passes_filters(j):
                    if link_is_live(j["link"]):
                        undated.append(j)
                    else:
                        failed.append(f"{src['company']}: הקישור למשרה '{j['title']}' לא נטען")
            if first_run:
                state["baselined"].append(src["company"])
                baselined_now.append(src["company"])
            continue

        for j in jobs:
            if not (is_recent(j, now) and passes_filters(j)):
                continue
            if not link_is_live(j["link"]):
                failed.append(f"{src['company']}: הקישור למשרה '{j['title']}' לא נטען")
                continue
            dated.append(j)

    def dedupe(items):
        seen, out = set(), []
        for j in items:
            if j["link"] not in seen:
                seen.add(j["link"])
                out.append(j)
        return out

    dated = sorted(dedupe(dated), key=lambda j: j["created"], reverse=True)
    undated = sorted(dedupe(undated), key=lambda j: j["first_seen"], reverse=True)
    send_telegram(build_message(dated, undated, checked, failed, baselined_now, now), dry)
    if not dry:
        save_state(state)


if __name__ == "__main__":
    main()
