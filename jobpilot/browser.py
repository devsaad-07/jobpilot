"""Browser sessions: a dedicated persistent Chrome profile (you log in to portals once with
`jobpilot login`), one context per application so each gets its own HAR file."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import Settings


CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class ProfileInUse(RuntimeError):
    pass


def profile_in_use(profile: Path) -> bool:
    """Chrome holds a SingletonLock symlink ('<host>-<pid>') in the profile while it's running.
    A lock left behind by a crash (pid no longer alive) is removed instead of blocking forever."""
    import os
    lock = profile / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return False
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
        os.kill(pid, 0)          # raises if that process is gone
        return True
    except ProcessLookupError:
        lock.unlink(missing_ok=True)
        return False
    except (OSError, ValueError):
        return True


def login_command(profile: Path, urls: list[str], chrome: str = CHROME_MAC) -> list[str]:
    """Plain Chrome (no automation flags) on jobpilot's profile, so sign-ins behave exactly like
    normal Chrome: Google sign-in, passkeys and 2FA work, and sites don't see an automated browser."""
    return [chrome, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", *urls]


# Playwright adds these by default. On macOS they make Chrome encrypt cookies with a dummy key instead of
# the login keychain, so cookies saved by `jobpilot login` (normal Chrome, real keychain) can't be read
# and every portal looks logged out, and the unreadable cookies get discarded. Dropping them makes the
# automated window read and write the same cookie store as the login window.
KEYCHAIN_FLAGS = ["--use-mock-keychain", "--password-store=basic"]


def launch_kwargs(s: Settings, har: Optional[Path] = None, headless: Optional[bool] = None) -> dict:
    b = s.cfg["browser"]
    kw = dict(
        user_data_dir=str(s.path("browser_profile")),
        headless=b.get("headless", False) if headless is None else headless,
        slow_mo=b.get("slow_mo_ms", 0),
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        accept_downloads=False,
        ignore_default_args=list(KEYCHAIN_FLAGS),
    )
    if b.get("channel"):
        kw["channel"] = b["channel"]
    if har is not None and b.get("record_har", True):
        kw.update(record_har_path=str(har), record_har_mode="minimal", record_har_content="embed")
    return kw


@contextmanager
def session(s: Settings, har: Optional[Path] = None, headless: Optional[bool] = None):
    from playwright.sync_api import sync_playwright

    if profile_in_use(s.path("browser_profile")):
        raise ProfileInUse("jobpilot's Chrome window is still open (from `jobpilot login`?). Quit it with Cmd+Q, then re-run.")
    b = s.cfg["browser"]
    kw = launch_kwargs(s, har, headless)
    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(**kw)
        except Exception as e:
            if kw.get("channel"):
                # Don't silently fall back to bundled Chromium: it uses a different keychain entry, so
                # your saved logins would be unreadable again.
                raise RuntimeError(f"Couldn't start Google Chrome ({e}). Install Chrome or set browser.channel "
                                   f"to null in config.yaml (you'd then need to log in again).") from e
            raise
        ctx.set_default_timeout(15000)
        ctx.set_default_navigation_timeout(b.get("nav_timeout_ms", 45000))
        try:
            yield ctx
        finally:
            ctx.close()  # flushes the HAR and the cookie store
