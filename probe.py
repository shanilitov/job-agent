#!/usr/bin/env python3
"""
Probe Workday career sites: does the site's own job-search endpoint answer,
how many Jerusalem jobs it lists, and what the posting-date field looks like.
Prints only; sends nothing.
"""
import requests

# (company, host, tenant, site)
WORKDAY = [
    ("MKS Instruments", "mksinst.wd1.myworkdayjobs.com", "mksinst", "MKSCareersEMEA"),
    ("Intel", "intel.wd1.myworkdayjobs.com", "intel", "External"),
    ("Medtronic", "medtronic.wd1.myworkdayjobs.com", "medtronic", "MedtronicCareers"),
    ("Merck (MSD)", "msd.wd5.myworkdayjobs.com", "msd", "SearchJobs"),
    ("Synamedia", "synamedia.wd3.myworkdayjobs.com", "synamedia", "Synamedia_Careers"),
    ("Cisco", "cisco.wd5.myworkdayjobs.com", "cisco", "Cisco_Careers"),
]

H = {"User-Agent": "Mozilla/5.0 (job-agent-probe)", "Accept": "application/json",
     "Content-Type": "application/json"}

for company, host, tenant, site in WORKDAY:
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    try:
        r = requests.post(url, json={"appliedFacets": {}, "limit": 20, "offset": 0,
                                     "searchText": "Jerusalem"}, headers=H, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"WD {company} | ERROR {e}")
        continue
    if r.status_code != 200:
        print(f"WD {company} | HTTP {r.status_code} | {r.text[:150]!r}")
        continue
    d = r.json()
    posts = d.get("jobPostings", [])
    jlm = [p for p in posts if "jerusalem" in (p.get("locationsText") or "").lower()]
    print(f"WD {company} | total={d.get('total')} | page={len(posts)} | jerusalem_on_page={len(jlm)}")
    for p in posts[:6]:
        print(f"  {p.get('title')} | {p.get('locationsText')} | {p.get('postedOn')} | {p.get('externalPath')}")
print("DONE")
