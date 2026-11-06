"""Packaging checks: the three version strings agree, the manifest is valid for openHop, and its defaults match
config.default.json (openHop seeds a new install from the file, the catalogue and settings editor read the manifest)."""

import json
import re
from pathlib import Path

from porchlight import __version__
from porchlight import main as pm

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "plugin" / "openhop-plugin.json").read_text())
PYPROJECT = (ROOT / "pyproject.toml").read_text()


def test_versions_agree():
    py = re.search(r'^version = "([^"]+)"', PYPROJECT, re.M).group(1)
    assert py == MANIFEST["version"] == __version__


def test_manifest_shape():
    assert MANIFEST["schema"] == 1
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", MANIFEST["id"]) and ".." not in MANIFEST["id"]
    assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", MANIFEST["version"])
    # the runtime entrypoint is the console script pyproject declares
    assert MANIFEST["runtime"] == {"type": "python", "entrypoint": "porchlight"}
    assert re.search(r'^\[project\.scripts\]\nporchlight = "porchlight\.main:main"', PYPROJECT, re.M)
    assert MANIFEST["ui"]["type"] == "application" and (ROOT / "plugin" / MANIFEST["ui"]["entry"]).is_file()
    # the wheel puts the manifest where openHop looks for it: share/openhop/plugins/<id>/
    assert f'"share/openhop/plugins/{MANIFEST["id"]}" = ' in PYPROJECT


def test_manifest_defaults_match_the_defaults_file():
    assert MANIFEST["config"]["defaults"] == json.loads((ROOT / "plugin" / "config.default.json").read_text())


def test_defaults_carry_no_credentials_or_local_settings():
    d = MANIFEST["config"]["defaults"]
    assert d["api_token"] == "" and d["area"] == "" and d["private"] == [] and d["local_repeaters"] == []
    assert d["reach_check_url"] == "" and d["reach_observer"] == ""


def test_page_loads_nothing_from_the_internet():
    page = (ROOT / "plugin" / "ui" / "index.html").read_text()
    assert not re.search(r'<(link|script)[^>]+(href|src)="https?://', page)


def test_remove_reports_when_it_is_done(tmp_path, monkeypatch):
    # The page revokes its API token only after the backend says the remove request is done.
    w = pm.Watcher(tmp_path)
    removed = []
    monkeypatch.setattr(w.porch, "remove", lambda: removed.append(True))
    w.cfg = {"light_request": {"id": "42", "action": "remove"}}
    w.porch_requests()
    assert removed and w.porch.view()["done_request"] == "42"
    w.porch_requests()  # the same request is never handled twice
    assert len(removed) == 1


def test_connect_is_the_only_way_to_get_a_token():
    page = (ROOT / "plugin" / "ui" / "index.html").read_text()
    assert "p-token" not in page


def test_page_keeps_the_token_until_the_remove_really_worked():
    page = (ROOT / "plugin" / "ui" / "index.html").read_text()
    fn = page[page.index("async function finishRemove"):]
    assert fn.index("if (!done) throw") < fn.index("revokeOwnToken") and fn.index("if (failed) throw") < fn.index("revokeOwnToken")
