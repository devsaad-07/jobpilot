"""Location eligibility: India onsite/hybrid, or remote roles that can be done from India."""
from __future__ import annotations

import re

INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|gurugram|gurgaon|noida|delhi|ncr|chennai|"
                   r"kolkata|ahmedabad|jaipur|kochi|indore|chandigarh|coimbatore|trivandrum|thiruvananthapuram)\b", re.I)
REMOTE = re.compile(r"\b(remote|work from (home|anywhere)|wfh|distributed|anywhere)\b", re.I)
REMOTE_REGION_OK = re.compile(r"\b(apac|asia|asia[- ]pacific|emea ?& ?apac|anywhere|worldwide|global(ly)?|any (time ?zone|country)|"
                              r"ist|gmt ?\+ ?5:?30|utc ?\+ ?5:?30)\b", re.I)
RESTRICTED = re.compile(
    r"\b(us|u\.s\.|usa|united states|canada|uk|united kingdom|eu|europe|emea|latam|germany|brazil|mexico|australia|"
    r"singapore|philippines|poland|spain|portugal|netherlands|israel)\b[- ](only|based|residents?)|"
    r"must (be|reside|live) (located |based )?in (the )?(us|u\.s\.|usa|united states|canada|uk|eu|europe)\b|"
    r"(authori[sz]ed|eligible|permitted) to work in the (us|u\.s\.|united states|uk|eu)|"
    r"(us|u\.s\.) (citizen|person)|security clearance|green card|"
    r"within (the )?(us|u\.s\.|usa|united states|eu|europe|uk) (time ?zones?|only)|"
    r"(pst|est|cst|mst|pacific|eastern|central) (time|hours) (required|overlap)", re.I)
FOREIGN_LOC = re.compile(r"\b(us|usa|u\.s\.|united states|canada|uk|united kingdom|europe|eu|emea|latam|americas|"
                         r"germany|france|spain|portugal|poland|netherlands|ireland|brazil|mexico|argentina|australia|"
                         r"new zealand|singapore|japan|philippines|israel|uae|dubai|sweden|norway|denmark|finland|"
                         r"switzerland|austria|italy|belgium|czechia|czech republic|romania|ukraine|serbia|greece|"
                         r"turkey|south africa|nigeria|kenya|egypt|china|hong kong|taiwan|korea|vietnam|indonesia|"
                         r"malaysia|thailand|colombia|chile|peru|costa rica|london|berlin|amsterdam|paris|dublin|"
                         r"stockholm|toronto|new york|san francisco|seattle|austin|[A-Z]{2}\s*,\s*(us|usa))\b", re.I)
TITLE_SPLIT = re.compile(r"\s*[|()\[\]–—,/]\s*|\s+-\s+")


def title_place(title: str) -> str | None:
    """'Senior Backend Engineer - Databases | Sweden | Remote' -> 'Sweden'. Only whole title segments count, so
    'US Payments Engineer' is not treated as a location."""
    for seg in TITLE_SPLIT.split(title or ""):
        seg = re.sub(r"^(remote|hybrid|onsite|on-site)\s+|\s+(remote|only|based)$", "", seg.strip(), flags=re.I)
        if seg and FOREIGN_LOC.fullmatch(seg) and not INDIA.search(seg):
            return seg
    return None
INDIA_ELIGIBLE = re.compile(r"\b(based|located|reside|residing|work(ing)?|hire|hiring|candidates?) (in|from) india\b|"
                            r"\bindia[- ](based|remote)\b|\bremote[,( -]+india\b", re.I)
RELOCATION = re.compile(r"(relocat(e|ion) (to|is) (required|mandatory)|must (be willing to )?relocate to|"
                        r"visa sponsorship (is )?(provided|available) to relocate)", re.I)


def location_verdict(location: str, description: str, remote_flag: bool | None, title: str = "") -> tuple[str, str]:
    loc = location or ""
    desc = description or ""
    place = title_place(title)
    if place and not INDIA.search(loc) and not INDIA_ELIGIBLE.search(desc):
        return "fail", f"title says '{place}'"
    in_india = bool(INDIA.search(loc))
    is_remote = bool(remote_flag) or bool(REMOTE.search(loc))
    if RELOCATION.search(desc) and not in_india:
        return "fail", "relocation outside India required"
    if in_india:
        return "pass", f"India location ({loc.strip()[:60]})"
    if is_remote:
        if INDIA_ELIGIBLE.search(desc) or INDIA_ELIGIBLE.search(loc):
            return "pass", "remote, India explicitly eligible"
        if FOREIGN_LOC.search(loc) and not REMOTE_REGION_OK.search(loc):
            return "fail", f"remote, region-locked to '{FOREIGN_LOC.search(loc).group(0)}'"
        if RESTRICTED.search(loc) or RESTRICTED.search(desc):
            m = RESTRICTED.search(loc) or RESTRICTED.search(desc)
            return "fail", f"remote but restricted: '{m.group(0)}'"
        if INDIA.search(desc) or REMOTE_REGION_OK.search(loc) or REMOTE_REGION_OK.search(desc):
            return "pass", "remote, India-eligible"
        return "unknown", "remote; India eligibility not stated"
    if not loc.strip():
        return "unknown", "no location"
    return "fail", f"location outside India ({loc[:60]})"
