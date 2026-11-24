#!/usr/bin/env python3
"""
Probe: open Jerusalem companies' own career pages and report which hiring
system each one uses (by the IDs embedded in the page). For Comeet pages,
also report the structure of the positions data. Prints a report only.
"""
import json
import re

import requests

CAREER_PAGES = {
    "Lightricks": "https://careers.lightricks.com/",
    "OrCam": "https://www.orcam.com/en-us/careers",
    "Hometalk": "https://www.careers.hometalk.com/",
    "OurCrowd": "https://www.ourcrowd.com/careers",
    "BrainsWay": "https://www.brainsway.com/careers/",
    "Sight Diagnostics": "https://www.sight.com/careers",
    "Squaretalk": "https://squaretalk.com/careers/",
    "Ex Libris": "https://exlibrisgroup.com/company/careers/",
    "Synamedia": "https://www.synamedia.com/careers/",
    "HUMAN Security": "https://www.humansecurity.com/company/careers",
    "Mprest": "https://mprest.com/careers/",
    "Imagry": "https://imagry.co/careers/",
    "Cyolo": "https://cyolo.io/careers",
    "Intel Israel": "https://jobs.intel.com/",
    "Teva": "https://careers.tevapharm.com/",
}

H = {"User-Agent": "Mozilla/5.0 (job-agent-probe)"}

PATTERNS = {
    "comeet": re.compile(r"comeet\.(?:com|co)/jobs/([\w.-]+)/([\w.]+)", re.I),
    "comeet-uid": re.compile(r"company-uid=\"?([\w.]+)|company_uid['\"]?\s*[:=]\s*['\"]([\w.]+)", re.I),
    "greenhouse": re.compile(r"greenhouse\.io/(?:embed/job_board\?for=|v1/boards/)?([\w-]+)", re.I),
    "lever": re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w-]+)", re.I),
    "workable": re.compile(r"apply\.workable\.com/([\w-]+)", re.I),
    "smartrecruiters": re.compile(r"smartrecruiters\.com/([\w-]+)", re.I),
    "workday": re.compile(r"([\w-]+)\.wd\d+\.myworkdayjobs\.com/([\w-]+)", re.I),
    "breezy": re.compile(r"([\w-]+)\.breezy\.hr", re.I),
}

comeet_pages = set()
for company, url in CAREER_PAGES.items():
    try:
        r = requests.get(url, headers=H, timeout=25, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        print(f"PAGE {company} | ERROR {e}")
        continue
    found = {}
    for name, rx in PATTERNS.items():
        hits = {m if isinstance(m, str) else "/".join(x for x in m if x) for m in rx.findall(r.text)}
        if hits:
            found[name] = sorted(hits)[:5]
    print(f"PAGE {company} | {r.status_code} | final={r.url} | {found}")
    for slug_uid in found.get("comeet", []):
        comeet_pages.add(slug_uid)

# Comeet: show the structure of the embedded positions data.
for slug_uid in sorted(comeet_pages):
    url = f"https://www.comeet.com/jobs/{slug_uid}"
    try:
        r = requests.get(url, headers=H, timeout=25)
    except Exception as e:  # noqa: BLE001
        print(f"COMEET {slug_uid} | ERROR {e}")
        continue
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);", r.text, re.S)
    if not m:
        print(f"COMEET {slug_uid} | {r.status_code} | no COMPANY_POSITIONS_DATA")
        continue
    data = json.loads(m.group(1))
    keys = sorted(data[0].keys()) if data else []
    jlm = [p for p in data if "jerusalem" in json.dumps(p.get("location") or {}).lower()]
    print(f"COMEET {slug_uid} | positions={len(data)} | jerusalem={len(jlm)} | keys={keys}")
    for p in jlm[:3]:
        print(f"  SAMPLE {p.get('name')} | loc={(p.get('location') or {}).get('name')} | "
              f"type={p.get('employment_type')} | time_updated={p.get('time_updated')} | "
              f"url={p.get('url_comeet_hosted_page') or p.get('url_active_page')}")
print("DONE")
