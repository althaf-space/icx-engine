from pathlib import Path
from icx_engine.testing.devices.device_backend import (
    Target, parse_targets, installed_engines, default_targets,
)


def test_target_label():
    assert Target("chromium").label() == "chromium"
    assert Target("webkit", "Pixel 7").label() == "webkit:Pixel 7"


def test_parse_targets_engines_and_device():
    ts = parse_targets("chromium, firefox , webkit:Pixel 7")
    assert ts == [Target("chromium"), Target("firefox"), Target("webkit", "Pixel 7")]


def test_parse_targets_ignores_blanks_and_bad_engines():
    ts = parse_targets("chromium,,notabrowser,firefox:iPhone 14")
    assert ts == [Target("chromium"), Target("firefox", "iPhone 14")]


def test_parse_targets_empty_is_empty():
    assert parse_targets("") == [] and parse_targets("   ") == []


def test_default_targets_is_chromium_desktop():
    assert default_targets() == [Target("chromium")]


def test_installed_engines_reads_browser_dirs(tmp_path):
    (tmp_path / "chromium-1140").mkdir()
    (tmp_path / "firefox-1450").mkdir()
    (tmp_path / "ffmpeg-1010").mkdir()          # not a browser engine
    got = installed_engines(tmp_path)
    assert "chromium" in got and "firefox" in got and "webkit" not in got
    assert "ffmpeg" not in got
