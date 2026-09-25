from pathlib import Path

import pytest

from jobpilot.browser import KEYCHAIN_FLAGS, ProfileInUse, launch_kwargs, login_command, profile_in_use, session


def test_login_command_is_plain_chrome(tmp_path):
    cmd = login_command(tmp_path, ["https://www.linkedin.com/feed/"], "/x/Chrome")
    assert cmd[0] == "/x/Chrome" and f"--user-data-dir={tmp_path}" in cmd
    assert not any("remote-debugging" in c or "enable-automation" in c for c in cmd)   # a normal, non-automated window


def test_open_profile_blocks_automation(settings):
    prof = settings.path("browser_profile")
    import os
    (prof / "SingletonLock").symlink_to(f"host-{os.getpid()}")   # a live process holds it
    assert profile_in_use(prof)
    with pytest.raises(ProfileInUse):
        with session(settings):
            pass


def test_stale_lock_is_cleared(settings):
    prof = settings.path("browser_profile")
    (prof / "SingletonLock").symlink_to("host-999999")          # crashed Chrome, pid gone
    assert profile_in_use(prof) is False and not (prof / "SingletonLock").is_symlink()


def test_automated_chrome_uses_real_keychain(settings):
    """Regression: Playwright's --use-mock-keychain made logins from `jobpilot login` unreadable."""
    kw = launch_kwargs(settings)
    assert "--use-mock-keychain" in kw["ignore_default_args"] and "--password-store=basic" in kw["ignore_default_args"]
    assert kw["user_data_dir"] == str(settings.path("browser_profile"))
    assert set(KEYCHAIN_FLAGS) <= set(kw["ignore_default_args"])
