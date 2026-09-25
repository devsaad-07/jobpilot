from jobpilot.browser import session
from jobpilot.portal_auth import login_state


def _page(settings, tmp_path, name, html):
    p = tmp_path / name
    p.write_text(html)
    return p.as_uri()


def test_signals(settings, tmp_path):
    logged_out = _page(settings, tmp_path, "home_out.html", "<header><a href='#'>Jobs</a><button>Sign in</button><a>Join now</a></header>")
    logged_in = _page(settings, tmp_path, "home_in.html", "<header><a href='#'>Jobs</a><img class='avatar-x' alt='me'></header>")
    wall = tmp_path / "authwall"
    wall.mkdir()
    (wall / "index.html").write_text("<p>Please sign in</p>")
    with session(settings, headless=True) as ctx:
        page = ctx.new_page()
        page.goto(logged_out)
        assert login_state(page, {"logged_in_check": ".nope"}) == (False, "page shows sign-in controls: ['Sign in', 'Join now']")
        page.goto(logged_in)
        ok, why = login_state(page, {"logged_in_check": ".nope"})
        assert ok and "no sign-in prompts" in why
        page.goto((wall / "index.html").as_uri())
        assert login_state(page, {})[0] is False                       # auth-wall URL
        # a session cookie beats a page that still shows a stray "Sign up" link
        ctx.add_cookies([{"name": "li_at", "value": "x", "domain": ".linkedin.com", "path": "/"}])
        page.goto(logged_out)
        ok, why = login_state(page, {"session_cookies": ["li_at"], "logged_in_check": ".nope"})
        assert ok and "li_at" in why
