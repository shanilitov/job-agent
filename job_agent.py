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
  python job_agent.py --no-save  # send, but do not update seen.json (test runs)

Environment variables (use GitHub Secrets, never put them in code):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import re
import sys
import time
import datetime as dt
from zoneinfo import ZoneInfo

import requests

IL = ZoneInfo("Asia/Jerusalem")
MAX_AGE_HOURS = int(os.environ.get("JOB_AGENT_MAX_AGE_HOURS", "72"))  # override only for test runs
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
    {
        # fullpath.com/careers is built on Comeet (company uid 54.002, seen in the page).
        "company": "Fullpath",
        "type": "comeet",
        "page": "https://www.comeet.com/jobs/fullpath/54.002",
        "careers_page": "https://www.fullpath.com/careers",
    },
    {
        # freightos.com/careers is built on Comeet (company uid F0.000, seen in the page).
        "company": "Freightos",
        "type": "comeet",
        "page": "https://www.comeet.com/jobs/freightos/F0.000",
        "careers_page": "https://www.freightos.com/careers/",
    },
    {
        # Plain careers page, no hiring system and no dates. Each job is a link to
        # /career/<name>/. The page lists the jobs under "Har Hotzvim, Jerusalem".
        "company": "AGAT Software",
        "type": "html",
        "page": "https://agatsoftware.com/careers/",
        "link_pattern": r"https://agatsoftware\.com/career/[\w-]+/?",
        "location": "Jerusalem (Har Hotzvim), per the company's careers page",
        "careers_page": "https://agatsoftware.com/careers/",
    },
    {
        "company": "Intel",
        "type": "workday",
        "host": "intel.wd1.myworkdayjobs.com", "tenant": "intel", "site": "External",
        "careers_page": "https://intel.wd1.myworkdayjobs.com/External",
    },
    {
        "company": "MKS Instruments",
        "type": "workday",
        "host": "mksinst.wd1.myworkdayjobs.com", "tenant": "mksinst", "site": "MKSCareersEMEA",
        "careers_page": "https://mksinst.wd1.myworkdayjobs.com/MKSCareersEMEA",
    },
    {
        # elbitsystemscareer.com loads all jobs from its own /cron/jobs.json.
        # City id 493 is shown on Elbit's job pages as "ירושלים והסביבה".
        "company": "Elbit Systems",
        "type": "elbit",
        "url": "https://elbitsystemscareer.com/cron/jobs.json",
        "city_id": "493",
        "careers_page": "https://elbitsystemscareer.com",
    },
    {
        # Zoho Recruit career site; the job table includes "Date Opened".
        "company": "Squaretalk",
        "type": "zoho",
        "page": "https://careers.squaretalk.com/careers",
        "careers_page": "https://careers.squaretalk.com/careers",
    },
    {
        # Phenom career site (owner of Ex Libris). Its own search endpoint returns
        # postedDate for each job.
        "company": "Clarivate",
        "type": "phenom",
        "base": "https://careers.clarivate.com",
        "ref_num": "CLACLAGB",
        "careers_page": "https://careers.clarivate.com",
    },
]

# Where first-seen dates are kept between runs (committed back by the workflow).
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

# The title must name a software-development role in the target stack...
INCLUDE_TITLE = re.compile(
    r"(full[\s-]?stack|front[\s-]?end|back[\s-]?end|software|developer|web|"
    r"\.net|c#|angular|react|node|python|java\b|typescript|"
    r"מפתח|תוכנה|פיתוח|פול\s?סטאק|פולסטאק)",
    re.IGNORECASE,
)
# ...and must not name a level or a domain outside the target.
EXCLUDE_TITLE = re.compile(
    r"\b(lead\w*|principal|staff|head|director|vp|manager|architect|"
    r"intern|student|junior|jr\.?|"
    r"embedded|firmware|hardware|asic|verification|validation|algorithm\w*|c\+\+|"
    r"מנהל\S*|ראש\s+צוות|סטודנט\S*|אלגוריתמ\S*|חומרה|משובצ\S*|הנדסאי\S*)",
    re.IGNORECASE,
)
# Senior roles are not dropped: they go to their own section of the message.
SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|בכיר\S*|מנוסה|מנוס\S*)(?=\W|$)", re.IGNORECASE)
YEARS_RE = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?years?", re.IGNORECASE)

# Staffing agencies are not in SOURCES at all, so they can never appear.
# This list is an extra safety net on the employer name returned by a source.
STAFFING_BLACKLIST = ["top-soft", "tops-soft", "manpower", "matrix", "ness"]


def html_to_text(raw):
    """HTML (possibly escaped twice) to plain text, one line per paragraph/bullet."""
    t = html.unescape(html.unescape(raw or ""))
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    lines = (" ".join(x.split()) for x in t.split("\n"))
    return "\n".join(x for x in lines if x)


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
                "description": html_to_text(p.get("content")),
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


def fetch_zoho(source):
    r = requests.get(source["page"], timeout=30, headers={"User-Agent": "Mozilla/5.0 (job-agent)"})
    r.raise_for_status()
    base = re.match(r"https?://[^/]+", source["page"]).group(0)
    jobs = []
    for row in re.findall(r'<tr[^>]*class="jobDetailRow"[^>]*>(.*?)</tr>', r.text, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        href = re.search(r"href='([^']+)'", row) or re.search(r'href="([^"]+)"', row)
        if len(cells) < 4 or not href:
            continue
        text = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        try:
            opened = dt.datetime.strptime(text[2], "%m/%d/%Y").replace(tzinfo=IL)
        except ValueError:
            continue  # no readable date -> never included
        jobs.append(
            {
                "company": source["company"],
                "title": text[1],
                "location": text[3],
                "all_locations": [],
                "commitment": text[0],
                "workplace": "",
                "created": opened.astimezone(dt.timezone.utc),
                "link": base + html.unescape(href.group(1)),
                "description": "",
            }
        )
    return jobs


def fetch_phenom(source):
    body = {
        "lang": "en_gb", "deviceType": "desktop", "country": "gb",
        "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "Most recent",
        "subsearch": "", "from": 0, "irs": False, "jobs": True, "counts": False,
        "all_fields": ["city"], "size": 100, "clearAll": False, "jdsource": "facets",
        "isSliderEnable": False, "siteType": "external", "keywords": "", "global": True,
        "selected_fields": {"city": ["Jerusalem"]}, "locationData": {},
        "refNum": source["ref_num"],
    }
    r = requests.post(source["base"] + "/widgets", json=body, timeout=30,
                      headers={"User-Agent": "Mozilla/5.0 (job-agent)"})
    r.raise_for_status()
    data = (r.json().get("refineSearch") or {}).get("data") or {}
    jobs = []
    for p in data.get("jobs", []):
        posted = p.get("postedDate")
        if not posted:
            continue  # no publish date -> never included
        jobs.append(
            {
                "company": source["company"],
                "title": (p.get("title") or "").strip(),
                "location": (p.get("cityStateCountry") or p.get("location") or "").strip(),
                "all_locations": [p.get("city") or ""],
                "commitment": p.get("type"),
                "workplace": (p.get("remote") or "") if isinstance(p.get("remote"), str) else "",
                "created": dt.datetime.strptime(posted[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc),
                "link": f"{source['base']}/job/{p.get('jobId') or p.get('reqId')}/",
                # Left empty on purpose: the full text is read from the job page itself.
                "description": "",
            }
        )
    return jobs


def fetch_html(source):
    """A company's own plain careers page: job titles only, no dates.
    Location (and type, if given) come from what the page says for all its jobs."""
    r = requests.get(source["page"], timeout=30, headers={"User-Agent": "Mozilla/5.0 (job-agent)"})
    r.raise_for_status()
    found = []  # (title, link)
    if "link_pattern" in source:
        for href, inner in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
            if re.fullmatch(source["link_pattern"], href):
                title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
                if title and title.lower() not in ("apply now", "apply"):
                    found.append((title, href))
    else:
        tag = source["heading_tag"]
        for inner in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", r.text, re.S | re.I):
            title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
            if title and title not in source.get("ignore_titles", []):
                found.append((title, source["page"]))
    jobs, seen = [], set()
    for title, link in found:
        if (title, link) in seen:
            continue
        seen.add((title, link))
        jobs.append(
            {
                "company": source["company"],
                "title": title,
                "location": source["location"],
                "all_locations": [],
                "commitment": source.get("commitment"),
                "workplace": "",
                "created": None,
                "link": link,
                # Several jobs can share one page; the title keeps their keys apart.
                "key": f"{link}#{title}",
                "description": "",
            }
        )
    return jobs


def _workday_age_days(posted_on):
    """Workday's own list text, e.g. 'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    t = (posted_on or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*days", t)
    if m:
        return 999 if "+" in t else int(m.group(1))
    return None


def _workday_call(method, url, h, **kw):
    """Workday rate-limits busy addresses (HTTP 429); wait and retry a few times."""
    for wait in (0, 20, 45, 90):
        if wait:
            time.sleep(wait)
        r = requests.request(method, url, headers=h, timeout=30, **kw)
        if r.status_code != 429:
            return r
    return r


def fetch_workday(source):
    """Workday career site. The list call is the one the site itself makes; the
    publish date, locations and job type come from each job's own detail record."""
    host, tenant, site = source["host"], source["tenant"], source["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    h = {"User-Agent": "Mozilla/5.0 (job-agent)", "Accept": "application/json",
         "Content-Type": "application/json"}
    posts, offset = [], 0
    while True:
        r = _workday_call("POST", f"{base}/jobs", h, json={"appliedFacets": {}, "limit": 20,
                                                          "offset": offset, "searchText": "Jerusalem"})
        r.raise_for_status()
        d = r.json()
        page = d.get("jobPostings", [])
        posts += page
        offset += 20
        if not page or offset >= (d.get("total") or 0) or offset >= 200:
            break
    jobs = []
    for p in posts:
        age = _workday_age_days(p.get("postedOn"))
        if age is None or age > MAX_AGE_HOURS / 24 + 1:
            continue  # not recent by Workday's own list -> skip the detail call
        if not INCLUDE_TITLE.search(p.get("title") or ""):
            continue
        time.sleep(1)
        r = _workday_call("GET", base + p["externalPath"], h)
        if r.status_code != 200:
            continue
        info = r.json().get("jobPostingInfo") or {}
        start = info.get("startDate")
        if not start:
            continue  # no publish date -> never included
        jobs.append(
            {
                "company": source["company"],
                "title": (info.get("title") or p.get("title") or "").strip(),
                "location": info.get("location") or "",
                "all_locations": list(info.get("additionalLocations") or []),
                "commitment": info.get("timeType"),
                "workplace": "",
                "created": dt.datetime.strptime(start[:10], "%Y-%m-%d").replace(tzinfo=IL)
                .astimezone(dt.timezone.utc),
                "link": info.get("externalUrl") or f"https://{host}/{site}{p['externalPath']}",
                "description": html_to_text(info.get("jobDescription")),
            }
        )
    return jobs


def fetch_elbit(source):
    r = requests.get(source["url"], timeout=60, headers={"User-Agent": "Mozilla/5.0 (job-agent)"})
    r.raise_for_status()
    jobs = []
    for p in r.json():
        props = {x.get("PropertyName"): x.get("Value") for x in p.get("extendedProperties") or []}
        if props.get("Cities") != source["city_id"] or p.get("status") != 1:
            continue
        opened = p.get("openDate")
        if not opened:
            continue  # no publish date -> never included
        jobs.append(
            {
                "company": source["company"],
                "title": (p.get("jobTitle") or "").strip(),
                "location": "ירושלים והסביבה",
                "all_locations": [],
                "commitment": p.get("employmentType"),
                "workplace": "",
                "created": dt.datetime.strptime(opened[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=IL)
                .astimezone(dt.timezone.utc),
                "link": f"https://elbitsystemscareer.com/job/?jid={p.get('jobId')}",
                "description": html_to_text(p.get("description")),
            }
        )
    return jobs


FETCHERS = {
    "elbit": fetch_elbit,
    "workday": fetch_workday,
    "html": fetch_html,
    "lever": fetch_lever,
    "greenhouse": fetch_greenhouse,
    "comeet": fetch_comeet,
    "zoho": fetch_zoho,
    "phenom": fetch_phenom,
}

# Experience levels (as the source names them) that are above the target.
EXCLUDE_LEVELS = {"senior", "management", "executive", "director"}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def is_recent(job, now):
    return (now - job["created"]) <= dt.timedelta(hours=MAX_AGE_HOURS)


def in_jerusalem(job):
    places = [job["location"]] + job["all_locations"]
    return any("jerusalem" in p.lower() or "ירושלים" in p for p in places)


def is_full_time(job):
    # None = the source has no employment-type field; the job is kept and the
    # message says the type was not stated, rather than guessing it.
    if job["commitment"] is None:
        return True
    return "full" in job["commitment"].lower()


def level_ok(job):
    """Keep the job or not; kept jobs get job["senior"] = True/False."""
    if not INCLUDE_TITLE.search(job["title"]) or EXCLUDE_TITLE.search(job["title"]):
        return False
    level = (job.get("level") or "").lower()
    if level in EXCLUDE_LEVELS - {"senior"}:
        return False
    if SENIOR_TITLE.search(job["title"]) or level == "senior":
        job["senior"] = True
        return True
    years = [int(m.group(1)) for m in YEARS_RE.finditer(job["description"])]
    if years and max(years) > MAX_YEARS_ALLOWED:
        return False
    job["senior"] = False
    return True


def years_note(job):
    """Return the sentence from the posting that states the years of experience, or None."""
    text = job.get("description") or ""
    m = YEARS_RE.search(text)
    if not m:
        return None
    # Sentence / bullet boundaries around the match.
    left = max(text.rfind(c, 0, m.start()) for c in ".•;\n")
    right_candidates = [i for i in (text.find(c, m.end()) for c in ".•;\n") if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    sentence = " ".join(text[left + 1:right].split()).strip(" -–:")
    if len(sentence) > 160:  # very long sentence: keep a window around the match
        start = max(0, m.start() - left - 1 - 60)
        sentence = "…" + sentence[start:start + 150].strip() + "…"
    return sentence


def not_staffing(job):
    name = job["company"].lower()
    return not any(b in name for b in STAFFING_BLACKLIST)


def _visible_text(page):
    page = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", page)
    return html_to_text(page)


def open_job_page(job):
    """A job counts as open only if its own page loads in this run (HTTP 200).
    The page also fills in details the job list did not have (description,
    employment type), always copied from the page itself."""
    url = job.get("link")
    if not url:
        return False
    try:
        r = requests.get(url, timeout=30, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (job-agent)"})
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    posting = None
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.text):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                posting = d
    if not job.get("description"):
        if posting and posting.get("description"):
            job["description"] = html_to_text(posting["description"])
        else:
            job["description"] = _visible_text(r.text)
    if not job.get("commitment") and posting and posting.get("employmentType"):
        et = posting["employmentType"]
        et = et[0] if isinstance(et, list) and et else et
        job["commitment"] = {"FULL_TIME": "Full-time", "PART_TIME": "Part-time"}.get(et, et)
    return True


def years_ok_after_details(job):
    """The years rule runs again once the job page filled in the description."""
    if job.get("senior"):
        return True
    years = [int(m.group(1)) for m in YEARS_RE.finditer(job.get("description") or "")]
    return not (years and max(years) > MAX_YEARS_ALLOWED)


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
def esc(t):
    return html.escape(str(t), quote=False)


def window_text():
    if MAX_AGE_HOURS % 24 == 0 and MAX_AGE_HOURS > 72:
        return f"ב-{MAX_AGE_HOURS // 24} הימים האחרונים"
    return f"ב-{MAX_AGE_HOURS} השעות האחרונות"


def job_lines(j, date_label, date_value):
    """One job. Only fields the source actually gives are shown."""
    out = ["", f'<b><a href="{html.escape(j["link"])}">{esc(j["title"])}</a></b>']
    out.append(f"🏢 {esc(j['company'])}")
    if j.get("location"):
        out.append(f"📍 {esc(j['location'])}")
    details = [x for x in (j.get("commitment"), j.get("workplace")) if x]
    if details:
        out.append("💼 " + " · ".join(esc(x) for x in details))
    out.append(f"📅 {date_label}: {date_value}")
    note = years_note(j)
    if note:
        out.append(f"🎓 מתוך המודעה: <i>{esc(note)}</i>")
    return out


def build_message(dated, undated, checked, failed, baselined_now, now):
    date_str = now.astimezone(IL).strftime("%d.%m.%Y %H:%M")
    lines = [f"<b>עדכון משרות</b> · {date_str}"]
    if os.environ.get("JOB_AGENT_TEST_LABEL"):
        lines = [f"<b>{esc(os.environ['JOB_AGENT_TEST_LABEL'])}</b>", ""] + lines
    reg_dated = [j for j in dated if not j["senior"]]
    reg_undated = [j for j in undated if not j["senior"]]
    sen_dated = [j for j in dated if j["senior"]]
    sen_undated = [j for j in undated if j["senior"]]

    def posted(j):
        return job_lines(j, "פורסם", j["created"].astimezone(IL).strftime("%d.%m.%Y"))

    def first_seen(j):
        return job_lines(j, "נראתה לראשונה", j["first_seen"].astimezone(IL).strftime("%d.%m.%Y"))

    lines.append("")
    if reg_dated:
        lines.append(f"✅ <b>{len(reg_dated)} משרות חדשות</b> {window_text()}:")
        for j in reg_dated:
            lines += posted(j)
    else:
        lines.append(f"אין משרות חדשות שמתאימות לך {window_text()}.")
    if reg_undated:
        lines.append("")
        lines.append(f"🆕 <b>{len(reg_undated)} משרות שנפתחו מאז הבדיקה הקודמת</b>")
        lines.append("<i>(באתר שלהן אין תאריך פרסום, אז מוצג היום שבו הסוכן ראה אותן לראשונה)</i>")
        for j in reg_undated:
            lines += first_seen(j)
    if sen_dated or sen_undated:
        lines.append("")
        lines.append(f"⭐ <b>משרות Senior ({len(sen_dated) + len(sen_undated)})</b> · מעל רמת הניסיון שהגדרת")
        for j in sen_dated:
            lines += posted(j)
        for j in sen_undated:
            lines += first_seen(j)
    lines.append("")
    lines.append("—")
    lines.append(f"<i>נבדקו {len(checked)} חברות: {esc(', '.join(checked)) if checked else 'אין'}</i>")
    if baselined_now:
        lines.append(
            f"<i>התחלתי לעקוב אחרי {esc(', '.join(baselined_now))}. "
            "המשרות שכבר פתוחות שם לא ידווחו כחדשות, רק משרות שייפתחו מעכשיו.</i>"
        )
    if failed:
        lines.append(f"⚠️ לא ניתן היה לבדוק: {esc(', '.join(failed))}")
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
            json={"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
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
        print(f"SOURCE {src['company']}: {len(jobs)} jobs, "
              f"{sum(1 for j in jobs if in_jerusalem(j))} in Jerusalem")
        if src["type"] == "html" and not jobs:
            failed.append(f"{src['company']}: לא נמצאו משרות בדף הקריירה "
                          "(אולי אין כרגע, ואולי מבנה הדף השתנה)")
        if src["type"] == "html":
            for j in jobs:
                print(f"  HTML {src['company']}: {j['title']} -> {j['link']}")

        if src["type"] in ("comeet", "html"):  # no publish date: track first-seen
            first_run = src["company"] not in state["baselined"]
            for j in jobs:
                key = j.get("key") or j["link"]
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
                    if not open_job_page(j):
                        failed.append(f"{src['company']}: הקישור למשרה '{j['title']}' לא נטען")
                    elif is_full_time(j) and years_ok_after_details(j):
                        undated.append(j)
            if first_run:
                state["baselined"].append(src["company"])
                baselined_now.append(src["company"])
            continue

        for j in jobs:
            if not (is_recent(j, now) and passes_filters(j)):
                continue
            if not open_job_page(j):
                failed.append(f"{src['company']}: הקישור למשרה '{j['title']}' לא נטען")
                continue
            if is_full_time(j) and years_ok_after_details(j):
                dated.append(j)

    def dedupe(items):
        seen, out = set(), []
        for j in items:
            k = j.get("key") or j["link"]
            if k not in seen:
                seen.add(k)
                out.append(j)
        return out

    dated = sorted(dedupe(dated), key=lambda j: j["created"], reverse=True)
    undated = sorted(dedupe(undated), key=lambda j: j["first_seen"], reverse=True)
    send_telegram(build_message(dated, undated, checked, failed, baselined_now, now), dry)
    if not dry and "--no-save" not in args:
        save_state(state)


if __name__ == "__main__":
    main()
