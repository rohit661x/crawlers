"""Find which ATS a company uses and its board slug, then optionally add it to config.toml.

Run:  python -m crawlers.careers.discover "Stripe" "Notion" [--url CAREERS_URL] [--add]

Evidence, strongest first:
  1. careers page   the company's own careers page links to the board (--url, else guessed)
  2. name match     a guessed slug exists AND the board's company name matches
  3. slug only      a guessed slug exists but the name couldn't be confirmed (never auto-added)
Boards with 0 jobs are never auto-added (usually abandoned). Always eyeball the board name:
a guessed careers domain can belong to a different company (ada.com is Ada Health, not Ada).
Only greenhouse / ashby / lever / smartrecruiters / workday are detected; big-tech custom
backends are configured by hand.
"""
import argparse, asyncio, re, tomllib
from difflib import SequenceMatcher
from pathlib import Path
import httpx

CONFIG = Path(__file__).resolve().parent / "config.toml"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SLUG_ATS = ("greenhouse", "ashby", "lever", "smartrecruiters")

# links to ATS boards inside a careers page
_PAGE_PATTERNS = {
    "greenhouse": re.compile(r"greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    "lever": re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)"),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    "workday": re.compile(r"https://([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+)"),
}
_NOT_SLUGS = {"embed", "v1", "boards", "job_board", "js", "api", "jobs", "careers", "static", "assets"}

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())

def _name_matches(want: str, got: str | None) -> bool:
    if not got:
        return False
    a = _norm(re.sub(r"\b(inc|llc|ltd|technologies|labs|jobs|careers|hq)\b", "", want, flags=re.I))
    b = _norm(re.sub(r"\b(inc|llc|ltd|technologies|labs|jobs|careers|hq)\b", "", got, flags=re.I))
    return bool(a and b) and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.8)

def slug_guesses(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    out = ["".join(words), "-".join(words), words[0] if words else ""]
    out += [w + suf for w in ("".join(words),) for suf in ("hq", "inc", "ai", "jobs", "careers")]
    if len(words) > 1 and words[-1] in ("ai", "labs", "inc", "technologies"):
        out.append("".join(words[:-1]))
    return list(dict.fromkeys(s for s in out if s))

async def probe(client, ats: str, slug: str) -> tuple[int, str | None] | None:
    """Returns (job_count, board_company_name) if the board exists, else None."""
    try:
        if ats == "greenhouse":
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            if r.status_code != 200:
                return None
            n = len(r.json().get("jobs", []))
            meta = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            return n, meta.json().get("name") if meta.status_code == 200 else None
        if ats == "ashby":
            r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if r.status_code != 200:
                return None
            n = len(r.json().get("jobs", []))
            page = await client.get(f"https://jobs.ashbyhq.com/{slug}", headers={"Accept": "text/html"})
            m = re.search(r'"organization":\{[^}]*?"name":"([^"]+)"', page.text)
            return n, m.group(1) if m else None
        if ats == "lever":
            r = await client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
            if r.status_code != 200:
                return None
            n = len(r.json())
            page = await client.get(f"https://jobs.lever.co/{slug}", headers={"Accept": "text/html"})
            m = re.search(r"<title>(.*?)</title>", page.text, re.S)
            return n, m.group(1).strip() if m else None
        if ats == "smartrecruiters":
            r = await client.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                                 params={"limit": 1})
            d = r.json() if r.status_code == 200 else {}
            if not d.get("totalFound"):
                return None  # SR returns 200 + empty for any slug
            return d["totalFound"], (d["content"][0].get("company") or {}).get("name")
        if ats == "workday":  # slug here is "host/site"
            host, site = slug.split("/", 1)[0], slug.rsplit("/", 1)[-1]
            r = await client.post(f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/jobs",
                                  json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            return (r.json().get("total", 0), None) if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None

async def scrape_careers(client, name: str, url: str | None) -> list[tuple[str, str]]:
    urls = [url] if url else [f"https://www.{s}.com/{p}" for s in slug_guesses(name)[:1] for p in ("careers", "jobs")]
    found = []
    for u in urls:
        try:
            r = await client.get(u, headers={"Accept": "text/html"})
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        # the page itself may have redirected straight onto a board
        text = str(r.url) + " " + r.text
        for ats, pat in _PAGE_PATTERNS.items():
            for slug in pat.findall(text):
                if slug.lower() not in _NOT_SLUGS:
                    found.append((ats, slug))
    return list(dict.fromkeys(found))

async def discover(client, name: str, url: str | None = None, allowed: set | None = None) -> list[dict]:
    """All boards found for `name`, best first. `allowed` limits which ATSs are probed."""
    cands = {}
    for ats, slug in await scrape_careers(client, name, url):
        if not allowed or ats in allowed:
            cands[(ats, slug)] = "careers page"
    for slug in slug_guesses(name):
        for ats in SLUG_ATS if not allowed else [a for a in SLUG_ATS if a in allowed]:
            cands.setdefault((ats, slug), None)
    keys = list(cands)
    results = await asyncio.gather(*(probe(client, ats, slug) for ats, slug in keys))
    out = []
    for (ats, slug), res in zip(keys, results):
        if res is None:
            continue
        count, board_name = res
        evidence = cands[(ats, slug)] or ("name match" if _name_matches(name, board_name) else "slug only")
        out.append({"name": name, "ats": ats, "slug": slug, "jobs": count,
                    "board_name": board_name, "evidence": evidence})
    rank = {"careers page": 0, "name match": 1, "slug only": 2}
    # an empty board is usually abandoned after a move to another ATS
    out.sort(key=lambda c: (c["jobs"] == 0, rank[c["evidence"]], -c["jobs"]))
    return out

def config_block(c: dict) -> str:
    lines = ["", "[[companies]]", f'name = "{c["name"]}"', f'ats = "{c["ats"]}"']
    if c["ats"] == "workday":  # full Workday listings cap at 2000; search for early-career instead
        lines.append(f'url = "https://{c["slug"]}"')
        lines.append('searches = ["intern", "new grad", "university"]')
    else:
        lines.append(f'slug = "{c["slug"]}"')
    return "\n".join(lines) + "\n"

def add_to_config(cands: list[dict], path: Path = CONFIG) -> list[str]:
    existing = {co["name"].lower() for co in tomllib.loads(path.read_text()).get("companies", [])}
    added = []
    with open(path, "a") as f:
        for c in cands:
            if c["name"].lower() in existing:
                continue
            f.write(config_block(c))
            existing.add(c["name"].lower())
            added.append(c["name"])
    tomllib.loads(path.read_text())  # fail loudly if we broke the file
    return added

async def run(names: list[str], url: str | None, allowed: set | None = None, sem_n: int = 6) -> dict[str, list[dict]]:
    sem = asyncio.Semaphore(sem_n)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": UA, "Accept": "application/json"}) as client:
        async def one(n):
            async with sem:
                return n, await discover(client, n, url if len(names) == 1 else None, allowed)
        return dict(await asyncio.gather(*(one(n) for n in names)))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="+")
    ap.add_argument("--url", help="careers page URL (only with a single name)")
    ap.add_argument("--add", action="store_true", help="append confident matches to config.toml")
    ap.add_argument("--ats", help="comma list: only accept these ATSs (e.g. greenhouse,ashby)")
    args = ap.parse_args()
    allowed = set(args.ats.split(",")) if args.ats else None

    found = asyncio.run(run(args.names, args.url, allowed))
    to_add = []
    for name, cands in found.items():
        cands = [c for c in cands if not allowed or c["ats"] in allowed]
        if not cands:
            print(f"✗ {name}: no board found")
            continue
        best = cands[0]
        mark = "✓" if best["evidence"] != "slug only" and best["jobs"] > 0 else "?"
        print(f"{mark} {name}: {best['ats']}/{best['slug']}  {best['jobs']} jobs  "
              f"[{best['evidence']}; board says {best['board_name']!r}]")
        for c in cands[1:4]:
            print(f"      also {c['ats']}/{c['slug']}  {c['jobs']} jobs  [{c['evidence']}; {c['board_name']!r}]")
        if best["evidence"] != "slug only" and best["jobs"] > 0:
            to_add.append(best)
    if args.add:
        added = add_to_config(to_add)
        print(f"\nadded to config: {', '.join(added) or 'nothing new'}")

if __name__ == "__main__":
    main()
