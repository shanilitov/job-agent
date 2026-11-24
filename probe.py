#!/usr/bin/env python3
"""
Probe: for the companies listed on the Made in JLM Jerusalem jobs board
(ei.fo/board/madeinjlm, 2026-11-24), try their likely board names on
Greenhouse, Lever (US/EU) and Ashby, and inspect known Comeet pages.
Reports which boards exist, how many Jerusalem jobs they have, and whether
those jobs carry a publish date. Prints only; sends nothing.
"""
import json
import re

import requests

# Employers from the board. Staffing / outsourcing firms are left out on purpose.
COMPANIES = [
    "Apple", "Altera", "Merck", "Teramount", "Alpha Tau", "Rafael", "Fullpath",
    "Lightricks", "Mobileye", "Elbit Systems", "Cross River", "Cielo Inertial Solutions",
    "MKS Instruments", "Midrag", "TIDE Technology", "Squaretalk", "Air Doctor",
    "BrightSource", "C2A", "Visionary.ai", "AGAT Software", "Bezeq", "CauseMatch",
    "Omnix Medical", "Ray Techniques", "VIZO", "Clarivate", "CorrActions", "Tesla",
    "Therapy Everywhere", "Triple Whale", "USR Electronic Systems", "VeracityAds",
    "200Apps", "Condor Pacific", "eggXYt", "eVision Systems", "Freightos", "GradTrain",
    "IDT", "Lidwave", "MDI Health", "Medtronic", "OurCrowd", "RSIP Vision", "RWS",
    "Stats Perform", "Valera Health", "Wellthy", "Zoharay",
]

# Comeet pages seen as apply links on that board: (company, slug/uid)
COMEET = [("Cross River", "crossriver/C7.00F")]

H = {"User-Agent": "Mozilla/5.0 (job-agent-probe)"}


def variants(name):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower())
    words = base.split()
    out = {"".join(words), "-".join(words), words[0]}
    return [v for v in out if v]


def jlm(s):
    return "jerusalem" in (s or "").lower()


def greenhouse(slug):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", headers=H, timeout=20)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs", [])
    j = [x for x in jobs if jlm((x.get("location") or {}).get("name"))
         or any(jlm(o.get("name")) for o in x.get("offices", []) or [])]
    return len(jobs), len(j), sum(1 for x in j if x.get("first_published")), [x.get("title") for x in j[:4]]


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
    return len(jobs), len(j), sum(1 for x in j if x.get("createdAt")), [x.get("text") for x in j[:4]]


def ashby(slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=H, timeout=20)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs", [])
    def locs(x):
        return [x.get("location") or ""] + [s.get("location", "") for s in x.get("secondaryLocations", []) or []]
    j = [x for x in jobs if any(jlm(l) for l in locs(x))]
    return len(jobs), len(j), sum(1 for x in j if x.get("publishedAt")), [x.get("title") for x in j[:4]]


SYSTEMS = [
    ("greenhouse", greenhouse),
    ("lever-us", lambda s: lever(s, False)),
    ("lever-eu", lambda s: lever(s, True)),
    ("ashby", ashby),
]

print("RESULT company | system | slug | total | jerusalem | jerusalem-with-date | samples")
for company in COMPANIES:
    hit = False
    for slug in variants(company):
        for name, fn in SYSTEMS:
            try:
                res = fn(slug)
            except Exception as e:  # noqa: BLE001
                print(f"ERR {company} {name} {slug}: {e}")
                continue
            if res is None:
                continue
            hit = True
            total, nj, dated, samples = res
            print(f"RESULT {company} | {name} | {slug} | {total} | {nj} | {dated} | {samples}")
    if not hit:
        print(f"NONE {company}")

for company, slug_uid in COMEET:
    url = f"https://www.comeet.com/jobs/{slug_uid}"
    try:
        r = requests.get(url, headers=H, timeout=25)
    except Exception as e:  # noqa: BLE001
        print(f"COMEET {company} | ERROR {e}")
        continue
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);\s*\n", r.text, re.S)
    if not m:
        idx = r.text.find("POSITIONS")
        print(f"COMEET {company} | {r.status_code} | no COMPANY_POSITIONS_DATA | around: {r.text[max(0, idx-100):idx+300]!r}")
        continue
    data = json.loads(m.group(1))
    keys = sorted(data[0].keys()) if data else []
    j = [p for p in data if "jerusalem" in json.dumps(p.get("location") or {}).lower()]
    print(f"COMEET {company} | positions={len(data)} | jerusalem={len(j)} | keys={keys}")
    for p in j[:4]:
        print(f"  SAMPLE {json.dumps({k: p.get(k) for k in keys if 'time' in k or 'date' in k or k in ('name', 'employment_type', 'experience_level', 'workplace_type', 'url_comeet_hosted_page')}, ensure_ascii=False)[:600]}")
print("DONE")
