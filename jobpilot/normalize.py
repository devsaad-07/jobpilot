"""Canonicalisation used by dedup: company names, titles, URLs."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_COMPANY_SUFFIX = re.compile(
    r"\b(private|pvt|limited|ltd|llp|llc|inc|incorporated|corp|corporation|co|company|gmbh|plc|technologies|technology|"
    r"tech|labs|software|solutions|services|systems|india|global|group|holdings|hq)\b\.?", re.I)
_SENIORITY = {
    r"\bsr\.?\b": "senior", r"\bsnr\b": "senior", r"\bsde\s*[- ]?\s*(3|iii)\b": "senior software engineer",
    r"\bsoftware development engineer\s*[- ]?\s*(3|iii)\b": "senior software engineer",
    r"\bswe\b": "software engineer", r"\bback[- ]end\b": "backend", r"\bfull[- ]stack\b": "fullstack",
    r"\bdev\b": "developer", r"\bengg?\b": "engineer",
}
_TITLE_NOISE = re.compile(
    r"\(.*?\)|\[.*?\]|\b(remote|hybrid|onsite|on-site|india|bengaluru|bangalore|hyderabad|pune|mumbai|gurugram|gurgaon|noida|"
    r"delhi|chennai|apac|urgent|immediate joiner[s]?|wfh|work from home|full[- ]time|permanent|contract)\b", re.I)
_TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|refid|trk|trackingid|src|source|gh_src|lever-source.*|lever-origin|"
                              r"currentjobid|eBP|refId|trackingId|position|pageNum|origin)$", re.I)


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def canon_company(name: str, aliases: dict[str, str] | None = None) -> str:
    """'Razorpay Software Pvt. Ltd.' -> 'razorpay'. `aliases` maps folded alias -> canonical."""
    f = fold(name)
    f = re.sub(r"^https?://", "", f).removeprefix("www.")
    f = re.sub(r"\.(co\.in|com|io|ai|co|in|net|org|markets|tech|app|dev|so|xyz)(/.*)?$", "", f)  # 'sahi.com' -> 'sahi'
    f = re.sub(r"[^a-z0-9&+ .]", " ", f)
    if aliases and f.strip() in aliases:
        return aliases[f.strip()]
    base = _COMPANY_SUFFIX.sub(" ", f)
    base = re.sub(r"[^a-z0-9]+", "", base) or re.sub(r"[^a-z0-9]+", "", f)
    if aliases and base in aliases:
        return aliases[base]
    return base


def build_alias_map(companies: list[dict]) -> dict[str, str]:
    m: dict[str, str] = {}
    for c in companies:
        canon = canon_company(c["name"])
        for a in [c["name"], *c.get("aliases", []), *c.get("slugs", [])]:
            m[fold(a)] = canon
            m[canon_company(a)] = canon
    return m


def canon_title(title: str) -> str:
    t = fold(title)
    t = _TITLE_NOISE.sub(" ", t)
    for pat, rep in _SENIORITY.items():
        t = re.sub(pat, rep, t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\b(i{1,3}|[123])\b", lambda m: {"i": "1", "ii": "2", "iii": "3"}.get(m.group(1), m.group(1)), t)
    return re.sub(r"\s+", " ", t).strip()


def canon_url(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", p.path)
    # LinkedIn: /jobs/view/<slug>-<id> and ?currentJobId=<id> collapse to the id
    if "linkedin.com" in host:
        m = re.search(r"(\d{8,})", path) or re.search(r"currentJobId=(\d+)", p.query)
        if m:
            return f"linkedin.com/jobs/view/{m.group(1)}"
    # ATS apply pages collapse onto the posting
    path = re.sub(r"/(apply|application)$", "", path)
    q = [(k, v) for k, v in parse_qsl(p.query) if not _TRACKING_PARAMS.match(k)]
    # Greenhouse embeds: ?gh_jid=123 is the identity
    return urlunsplit(("", host, path, urlencode(sorted(q)), "")).lstrip("/")


def ats_from_url(url: str) -> str:
    h = urlsplit(url or "").netloc.lower()
    if "greenhouse" in h or "gh_jid=" in (url or ""):
        return "greenhouse"
    if "lever.co" in h:
        return "lever"
    if "ashbyhq" in h:
        return "ashby"
    if "workable" in h:
        return "workable"
    if "myworkdayjobs" in h or "workday" in h:
        return "workday"
    return ""
