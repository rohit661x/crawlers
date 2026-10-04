"""Decide which jobs are worth a Telegram ping.

Config (global [filters], overridable per company with a `filters` table):
  title_include  any match required (empty = everything). Plain terms match whole words,
                 case-insensitive; prefix with "re:" for a raw regex. Used for level (intern, new grad).
  role_include   any match also required (empty = any role). Same syntax. Used for function.
  title_exclude  any match rejects.
  locations      any substring match in location required (empty = anywhere).
  remote_ok      remote jobs with no geographic restriction ("Remote", "Anywhere", blank)
                 pass the location check. Regional remote ("Remote - Canada") is
                 handled by `locations` like any other location.
"""
import re

_UNRESTRICTED = re.compile(r"\b(remote|anywhere|worldwide|global|work from home|wfh)\b|[\s,;/()|·-]+", re.I)

def _compile(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    parts = [t[3:] if t.startswith("re:") else rf"\b{re.escape(t)}\b" for t in terms]
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)

class JobFilter:
    def __init__(self, cfg: dict):
        self.include = _compile(cfg.get("title_include", []))
        self.role = _compile(cfg.get("role_include", []))
        self.exclude = _compile(cfg.get("title_exclude", []))
        self.locations = [l.lower() for l in cfg.get("locations", [])]
        self.remote_ok = cfg.get("remote_ok", True)

    def matches(self, job) -> bool:
        """job: anything with title/location/remote (a Job or a sqlite Row)."""
        get = (lambda k: getattr(job, k)) if hasattr(job, "title") else job.__getitem__
        title, location, remote = get("title"), get("location") or "", get("remote")
        if self.include and not self.include.search(title or ""):
            return False
        if self.role and not self.role.search(title or ""):
            return False
        if self.exclude and self.exclude.search(title or ""):
            return False
        if self.locations:
            loc = location.lower()
            anywhere = (bool(remote) or "remote" in loc) and not _UNRESTRICTED.sub("", loc)
            if not (any(l in loc for l in self.locations) or (self.remote_ok and anywhere)):
                return False
        return True

def build_filters(cfg: dict) -> tuple[JobFilter, dict[str, JobFilter]]:
    """Global filter plus per-company overrides (company `filters` keys replace global keys)."""
    base = cfg.get("filters", {})
    per_company = {co["name"]: JobFilter({**base, **co["filters"]})
                   for co in cfg.get("companies", []) if "filters" in co}
    return JobFilter(base), per_company
