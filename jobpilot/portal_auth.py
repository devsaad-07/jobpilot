"""Is this portal session logged in? Several independent signals, because one guessed CSS selector
breaks every time a portal redesigns its header.

Order (first decisive signal wins):
  1. redirected to a login / auth-wall URL          → logged out
  2. a known session cookie is present (li_at, ...) → logged in
  3. the portal's `logged_in_check` selector matches → logged in
  4. visible "Sign in / Log in / Join now" controls  → logged out
  5. none of the above                               → logged in (no login prompt anywhere on the page)
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

LOGIN_URL_RX = re.compile(r"/(login|signin|sign-in|sign_in|uas/login|authwall|checkpoint|nlogin|signup|sign-up|register)"
                          r"(/|$|\?|#|-)", re.I)
LOGIN_TEXT_JS = r"""
() => {
  const rx = /^\s*(sign in|log in|login|join now|sign up|register|get started|continue with google)\s*$/i;
  const vis = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  return [...document.querySelectorAll('a, button, [role=button]')]
    .filter(e => rx.test((e.innerText || e.getAttribute('aria-label') || '').trim()) && vis(e))
    .map(e => (e.innerText || e.getAttribute('aria-label')).trim()).slice(0, 5);
}
"""


def login_state(page, pc: dict) -> tuple[bool, str]:
    """-> (logged_in, evidence)."""
    path = urlsplit(page.url).path
    if LOGIN_URL_RX.search(path + "/"):
        return False, f"redirected to a login page ({page.url[:100]})"
    wanted = pc.get("session_cookies") or []
    if wanted:
        have = {c["name"] for c in page.context.cookies()}
        hit = [c for c in wanted if c in have]
        if hit:
            return True, f"session cookie {hit[0]} present"
    sel = pc.get("logged_in_check")
    if sel:
        try:
            if page.locator(sel).count() > 0:
                return True, "logged-in marker found on page"
        except Exception:
            pass
    try:
        prompts = page.evaluate(LOGIN_TEXT_JS)
    except Exception:
        prompts = []
    if prompts:
        return False, f"page shows sign-in controls: {prompts}"
    return True, "no sign-in prompts on the page"
