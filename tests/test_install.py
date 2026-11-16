"""Install and update paths: finding openHop's API on any install (Pi, Home Assistant add-on, moved port) and the
self-update offered to GitHub installs. No network: GitHub is a fake opener."""

import hashlib
import io
import json
from pathlib import Path

import pytest

from porchlight import __version__
from porchlight import main as pm
from porchlight import updater as up

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return (str(p),)


# ---- openHop's API address ----
def test_default_port_when_no_config(tmp_path):
    assert pm.resolve_api_base({}, (str(tmp_path / "missing.yaml"),)) == "http://127.0.0.1:8000"


def test_port_read_from_openhop_config(tmp_path):
    paths = write(tmp_path, "repeater:\n  node_name: x\nhttp:\n  enabled: true\n  host: 0.0.0.0\n  port: 8080  # moved\nweb:\n  port: 1\n")
    assert pm.resolve_api_base({}, paths) == "http://127.0.0.1:8080"


def test_specific_bind_address_is_used(tmp_path):
    assert pm.resolve_api_base({}, write(tmp_path, "http:\n  host: '192.168.1.5'\n  port: 9000\n")) == "http://192.168.1.5:9000"
    assert pm.resolve_api_base({}, write(tmp_path, "http:\n  host: fd00::5\n  port: 9000\n")) == "http://[fd00::5]:9000"


def test_other_sections_port_is_ignored(tmp_path):
    assert pm.resolve_api_base({}, write(tmp_path, "mqtt:\n  port: 1883\nhttp:\n  enabled: true\n")) == "http://127.0.0.1:8000"


def test_owner_setting_wins(tmp_path):
    paths = write(tmp_path, "http:\n  port: 8080\n")
    assert pm.resolve_api_base({"api_base": "http://10.0.0.2:8000/"}, paths) == "http://10.0.0.2:8000"


def test_default_setting_is_auto():
    manifest = json.loads((ROOT / "plugin" / "openhop-plugin.json").read_text())
    assert manifest["config"]["defaults"]["api_base"] == ""
    assert manifest["config"]["defaults"]["update_request"] == {}


# ---- updates for GitHub installs ----
WHEEL = b"PK fake wheel bytes"
SHA = hashlib.sha256(WHEEL).hexdigest()


def bump(v):
    a, b, c = up.parse_version(v)
    return f"{a}.{b}.{c + 1}"


NEXT = bump(__version__)
NAME = f"porchlight_openhop_plugin-{NEXT}-py3-none-any.whl"
DL = f"https://github.com/{up.REPO}/releases/download/v{NEXT}/"


def release(digest=True, **over):
    wheel = {"name": NAME, "browser_download_url": DL + NAME}
    if digest:
        wheel["digest"] = "sha256:" + SHA
    rel = {"tag_name": f"v{NEXT}", "draft": False, "prerelease": False, "html_url": "https://example.invalid/r",
           "assets": [wheel, {"name": "catalogue-entry.json", "browser_download_url": DL + "catalogue-entry.json"}]}
    rel.update(over)
    return rel


def opener(files):
    seen = []

    def open_(req, timeout=30):
        seen.append(req.full_url)
        body = files[req.full_url]
        return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())

    open_.seen = seen
    return open_


def test_versions_compare():
    assert up.newer("0.10.10", "0.10.9") and up.newer("v1.0.0", "0.99.99")
    assert not up.newer("0.10.2", "0.10.2") and not up.newer("junk", "0.1.0")


def test_latest_uses_github_digest():
    info = up.latest_release(opener({up.RELEASES_URL: release()}))
    assert info == {"version": NEXT, "wheel_name": NAME, "wheel_url": DL + NAME, "sha256": SHA,
                    "page": "https://example.invalid/r"}


def test_latest_falls_back_to_catalogue_entry():
    files = {up.RELEASES_URL: release(digest=False), DL + "catalogue-entry.json": {"version": NEXT, "sha256": SHA}}
    assert up.latest_release(opener(files))["sha256"] == SHA


def test_latest_refuses_wheels_hosted_elsewhere():
    rel = release()
    rel["assets"][0]["browser_download_url"] = "https://evil.example/" + NAME
    with pytest.raises(ValueError):
        up.latest_release(opener({up.RELEASES_URL: rel}))


def test_latest_ignores_prereleases():
    with pytest.raises(ValueError):
        up.latest_release(opener({up.RELEASES_URL: release(prerelease=True)}))


def test_download_checks_the_sha(tmp_path):
    info = up.latest_release(opener({up.RELEASES_URL: release()}))
    path = up.download(info, tmp_path, opener({DL + NAME: WHEEL}))
    assert path.name == NAME and path.read_bytes() == WHEEL
    with pytest.raises(ValueError, match="checksum"):
        up.download(info, tmp_path / "bad", opener({DL + NAME: b"tampered"}))
    assert not list((tmp_path / "bad").glob("*.whl"))


def test_page_request_downloads_and_reports(tmp_path, monkeypatch):
    files = {up.RELEASES_URL: release(), DL + NAME: WHEEL}
    u = up.Updater(tmp_path, opener(files))
    monkeypatch.setattr(up.threading, "Thread", lambda target, args, **kw: type("T", (), {"start": lambda s: target(*args)})())
    u.tick({"id": "7", "action": "download"})
    v = u.view()
    assert v["available"] and v["latest"] == NEXT and v["ready"] == NEXT and v["done_request"] == "7" and not v["error"]
    assert Path(v["wheel_path"]).read_bytes() == WHEEL
    calls = len(files)
    u.tick({"id": "7", "action": "download"})  # the same request is never handled twice
    assert v["checked_at"] and u.view()["done_request"] == "7" and calls == len(files)


def test_failure_is_reported_not_raised(tmp_path, monkeypatch):
    def broken(req, timeout=30):
        raise OSError("no internet")

    u = up.Updater(tmp_path, broken)
    monkeypatch.setattr(up.threading, "Thread", lambda target, args, **kw: type("T", (), {"start": lambda s: target(*args)})())
    u.tick({"id": "8", "action": "check"})
    assert "no internet" in u.view()["error"] and u.view()["done_request"] == "8" and not u.view()["busy"]


def test_repository_is_not_a_setting():
    manifest = json.loads((ROOT / "plugin" / "openhop-plugin.json").read_text())
    assert not any("repo" in k for k in manifest["config"]["defaults"])


def test_page_offers_update_only_for_github_installs():
    page = (ROOT / "plugin" / "ui" / "index.html").read_text()
    assert "managed === false && u.available" in page
    fn = page[page.index('$("update").onclick'):]
    assert fn.index("u.ready !== u.latest") < fn.index("/api/plugins/install")
