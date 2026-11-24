#!/usr/bin/env python3
"""
Probe candidate career boards: which company slugs exist on which system,
whether they have Jerusalem jobs, and whether each job carries a publish date.
Prints a report only; sends nothing.
"""
import requests

CANDIDATES = [
    "triplewhale", "lightricks", "humansecurity", "human", "orcam", "squaretalk",
    "controlup", "bigid", "melio", "pagaya", "oktopost", "autofleet", "plusgrade",
    "vonage", "mixtiles", "brainsway", "briefcam", "exlibris", "synamedia",
    "cloudinary", "riskified", "appsflyer", "kaltura", "sapiens", "hibob",
    "lemonade", "rapyd", "honeybook", "similarweb", "sightdiagnostics", "sight",
    "ourcrowd", "cyolo", "imagry", "mobileye", "intel", "cisco", "ready",
    "hubsecurity", "kramer", "riverside", "papayaglobal", "gong", "forter",
    "nayax", "tipalti", "yotpo", "via", "optibus", "fireblocks", "unit",
    "unframe", "jfrog", "snyk", "armis", "catonetworks", "wiz", "monday",
    "bizzabo", "imperva", "checkmarx", "payoneer", "playtika", "lusha",
    "zesty", "spot", "sentra", "upwind", "aqua", "scalebase", "bmc",
]

H = {"User-Agent": "job-agent-probe"}


def jlm(s):
    return "jerusalem" in (s or "").lower()


def greenhouse(slug):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", headers=H, timeout=20)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs", [])
    j = [x for x in jobs if jlm((x.get("location") or {}).get("name"))
         or any(jlm(o.get("name")) for o in x.get("offices", []) or [])]
    dated = sum(1 for x in j if x.get("first_published"))
    return len(jobs), j, dated, [(x.get("title"), x.get("first_published"), x.get("updated_at")) for x in j[:3]]


def lever(slug, eu):
    host = "api.eu.lever.co" if eu else "api.lever.co"
    r = requests.get(f"https://{host}/v0/postings/{slug}?mode=json", headers=H, timeout=20)
    if r.status_code != 200:
        return None
    jobs = r.json()
    if not isinstance(jobs, list):
        return None
    def locs(x):
        c = x.get("categories") or {}
        return [c.get("location") or ""] + (c.get("allLocations") or [])
    j = [x for x in jobs if any(jlm(l) for l in locs(x))]
    dated = sum(1 for x in j if x.get("createdAt"))
    return len(jobs), j, dated, [(x.get("text"), x.get("createdAt")) for x in j[:3]]


def ashby(slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=H, timeout=20)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs", [])
    def locs(x):
        return [x.get("location") or ""] + [s.get("location", "") for s in x.get("secondaryLocations", []) or []]
    j = [x for x in jobs if any(jlm(l) for l in locs(x))]
    dated = sum(1 for x in j if x.get("publishedAt"))
    return len(jobs), j, dated, [(x.get("title"), x.get("publishedAt")) for x in j[:3]]


SYSTEMS = [
    ("greenhouse", greenhouse),
    ("lever-us", lambda s: lever(s, False)),
    ("lever-eu", lambda s: lever(s, True)),
    ("ashby", ashby),
]

print("RESULT system | slug | total jobs | jerusalem jobs | jerusalem jobs with publish date | samples")
for slug in CANDIDATES:
    for name, fn in SYSTEMS:
        try:
            res = fn(slug)
        except Exception as e:  # noqa: BLE001
            print(f"ERR {name} {slug}: {e}")
            continue
        if res is None:
            continue
        total, j, dated, samples = res
        print(f"RESULT {name} | {slug} | {total} | {len(j)} | {dated} | {samples}")
print("DONE")
