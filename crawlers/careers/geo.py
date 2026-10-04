"""Is a free-text job location in the US or Canada?

ATS location strings vary wildly: "Redmond, WA, US", "US, CA, Santa Clara", "Toronto, ON",
"US-CA-Menlo Park", "Seattle, Washington, USA", "London, England, GBR", "REMOTE - AMERICAS",
"Perth, WA, AU", "3 Locations". Two-letter codes collide (IN = India/Indiana, WA = Washington/
Western Australia), so a code counts as a country only where countries sit: alone, last of 3+
parts, or before a dash ("US-CA-..."). "City, XX" reads XX as a state/province.

in_us_ca() returns True (some part of it is US/Canada), False (only elsewhere) or
None (can't tell: "Hybrid", "2 Locations", unknown city). Callers let None through.
"""
import re

US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
                "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
CA_PROVINCES = set("ON BC QC AB MB NS NB NL PE SK YT NT NU".split())
HOME_NAMES = {
    "united states", "united states of america", "usa", "u.s.", "u.s.a.", "us", "america", "americas",
    "north america", "canada", "can",
    # states / provinces spelled out
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia", "ontario", "british columbia",
    "quebec", "québec", "alberta", "manitoba", "nova scotia", "new brunswick", "saskatchewan",
    "newfoundland and labrador",
    # cities that often appear without a state
    "san francisco", "sf", "sf bay area", "bay area", "nyc", "new york city", "seattle", "boston",
    "austin", "chicago", "chi", "los angeles", "la", "mountain view", "sunnyvale", "menlo park",
    "palo alto", "cupertino", "redmond", "san jose", "santa clara", "san mateo", "san diego",
    "pittsburgh", "atlanta", "denver", "washington dc", "washington, d.c.", "miami", "portland",
    "toronto", "vancouver", "montreal", "montréal", "waterloo", "ottawa", "calgary", "edmonton",
    "kitchener", "starbase", "hawthorne", "bellevue", "kirkland", "halifax", "philadelphia", "raleigh",
    "south san francisco", "us west", "us east", "us central", "dallas", "ann arbor", "houston", "amer",
    "phoenix", "salt lake city", "detroit", "minneapolis", "nashville", "san antonio", "st. louis",
}
FOREIGN_NAMES = {
    "united kingdom", "uk", "england", "scotland", "wales", "ireland", "germany", "france", "spain",
    "italy", "netherlands", "belgium", "luxembourg", "switzerland", "austria", "sweden", "norway",
    "denmark", "finland", "poland", "portugal", "czech republic", "czechia", "romania", "hungary",
    "greece", "turkey", "israel", "india", "china", "japan", "korea", "korea, republic of", "south korea",
    "taiwan", "hong kong", "singapore", "australia", "new zealand", "brazil", "mexico", "argentina",
    "chile", "colombia", "peru", "philippines", "vietnam", "thailand", "malaysia", "indonesia",
    "united arab emirates", "uae", "saudi arabia", "qatar", "egypt", "south africa", "nigeria", "kenya",
    "armenia", "ukraine", "serbia", "bulgaria", "estonia", "latvia", "lithuania", "emea", "apac", "latam",
    "europe", "asia",
    # cities that often appear without a country
    "london", "dublin", "paris", "berlin", "munich", "zurich", "zürich", "amsterdam", "tel aviv",
    "bangalore", "bengaluru", "hyderabad", "pune", "chennai", "gurgaon", "gurugram", "noida", "mumbai",
    "tokyo", "sydney", "melbourne", "shanghai", "beijing", "shenzhen", "seoul", "taipei", "warsaw",
    "krakow", "madrid", "barcelona", "milan", "stockholm", "copenhagen", "lisbon", "prague", "sao paulo",
    "são paulo", "mexico city", "yerevan", "cambridge, uk", "costa rica", "the netherlands", "kuala lumpur",
    "belgrade", "cdmx", "taipei city", "rotterdam", "stuttgart", "dubai", "abu dhabi", "cambodia",
    "slovenia", "slovakia", "iceland", "reykjavík", "croatia", "cyprus", "malta", "pakistan", "bangladesh",
    "sri lanka", "morocco", "uruguay", "ecuador", "guatemala", "panama", "guadalajara", "bogota", "bogotá", "buenos aires", "lagos", "nairobi",
}
# ISO 3166 codes for foreign countries likely to show up (alpha-2 and alpha-3)
FOREIGN_ISO2 = set("GB UK IE DE FR ES IT NL BE LU CH AT SE NO DK FI PL PT CZ RO HU GR TR IL IN CN JP KR TW "
                   "HK SG AU NZ BR MX AR CL CO PE PH VN TH MY ID AE SA QA EG ZA NG KE AM UA RS BG EE LV LT".split())
FOREIGN_ISO3 = set("GBR IRL DEU FRA ESP ITA NLD BEL LUX CHE AUT SWE NOR DNK FIN POL PRT CZE ROU HUN GRC TUR "
                   "ISR IND CHN JPN KOR TWN HKG SGP AUS NZL BRA MEX ARG CHL COL PER PHL VNM THA MYS IDN ARE "
                   "SAU QAT EGY ZAF NGA KEN ARM UKR SRB BGR EST LVA LTU SVK SVN HRV ISL CRI URY".split())
HOME_CODES = {"US", "USA", "CA", "CAN"}

_SEGMENTS = re.compile(r"\s*(?:;|\||·|\n| / | or )\s*")
_DASH_CODE = re.compile(r"^([A-Za-z]{2,3})\s*-\s*(.*)$")

def _classify(part: str, *, country_slot: bool) -> str | None:
    """'home' | 'foreign' | None for one comma/dash part of a location."""
    p = part.strip().strip("()").strip()
    low = re.sub(r"\s*\(.*?\)", "", p.lower()).strip()  # "new york, ny (hq)" -> drop "(hq)"
    if not low:
        return None
    if low in HOME_NAMES:
        return "home"
    if low in FOREIGN_NAMES:
        return "foreign"
    if re.fullmatch(r"[A-Za-z]{3}", p) and p.isupper():
        return "home" if p in HOME_CODES else "foreign" if p in FOREIGN_ISO3 else None
    if re.fullmatch(r"[A-Za-z]{2}", p):
        code = p.upper()
        if country_slot:
            return "home" if code in HOME_CODES else "foreign" if code in FOREIGN_ISO2 else None
        if code in US_STATES or code in CA_PROVINCES:
            return "home"
        return "foreign" if code in FOREIGN_ISO2 else None
    if re.search(r"\b(remote|anywhere|worldwide|global)\b", low):
        # "Remote - US", "Remote, Canada": the qualifier decides; bare "Remote" is unknown
        rest = re.sub(r"\b(remote|anywhere|worldwide|global|only|first|friendly)\b|[-–,:()]", " ", low).split()
        return _classify(" ".join(rest), country_slot=True) if rest else None
    return None

_HOME_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(n) for n in HOME_NAMES if len(n) > 3), key=len, reverse=True)) + r")\b")
_FOREIGN_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(n) for n in FOREIGN_NAMES if len(n) > 3), key=len, reverse=True)) + r")\b")
_US_TOKEN = re.compile(r"\b(US|USA|U\.S\.)\b")  # case-sensitive: "Remote in the US", "PA United States"

def _scan(seg: str) -> str | None:
    """Last resort for phrases: "Toronto Headquarters", "London Office", "Remote in the US"."""
    low = seg.lower()
    home = bool(_HOME_RE.search(low) or _US_TOKEN.search(seg))
    foreign = bool(_FOREIGN_RE.search(low))
    return "home" if home else "foreign" if foreign else None

def _segment(seg: str) -> str | None:
    seg = seg.strip()
    m = _DASH_CODE.match(seg)
    if m and m.group(1).isupper():  # "US-CA-Menlo Park", "BR-Brazil-Remote"
        first = _classify(m.group(1), country_slot=True)
        if first:
            return first
    parts = [p for p in re.split(r"\s*,\s*|\s+-\s+|\s+–\s+", seg) if p.strip()]
    slots, verdicts = [], []
    for i, part in enumerate(parts):
        slot = len(parts) == 1 or (len(parts) >= 3 and i in (0, len(parts) - 1))
        v = _classify(part, country_slot=slot)
        (slots if slot else verdicts).append(v)
    # the country position outranks state codes: "Perth, WA, AU" is Australia, not Washington
    if any(slots):
        if "home" in slots and "foreign" not in slots:
            return "home"
        if "foreign" in slots and "home" not in slots:
            return "foreign"
        return slots[-1] or slots[0]
    if "home" in verdicts:
        return "home"
    if "foreign" in verdicts:
        return "foreign"
    return _scan(seg)

def in_us_ca(location: str | None) -> bool | None:
    if not location or not location.strip():
        return None
    verdicts = [_segment(s) for s in _SEGMENTS.split(location) if s.strip()]
    if "home" in verdicts:
        return True
    if "foreign" in verdicts:
        return False
    return None
