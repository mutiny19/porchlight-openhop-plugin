"""Updates for installs that openHop doesn't manage.

openHop checks for updates only for plugins installed from its catalogue. A wheel uploaded from the Plugins page (a
GitHub download) is a "local install" with no repository, so it would never hear of a new version. For those installs
only, the page asks the backend to look at this project's GitHub releases, download the newer wheel and check its
SHA-256; the page then installs it through openHop's own plugin install (POST /api/plugins/install).

The repository is a code constant, never a setting, so nobody can point an install at a different project.
Catalogue installs are left to openHop, which only offers versions the catalogue approved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__

log = logging.getLogger("porchlight.updater")

REPO = "mutiny19/porchlight-openhop-plugin"
DIST = "porchlight_openhop_plugin"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
USER_AGENT = f"porchlight-openhop-plugin/{__version__} (+https://github.com/{REPO})"
CHECK_EVERY = 24 * 3600
MAX_WHEEL = 20 * 1024 * 1024
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(v: str) -> tuple[int, int, int] | None:
    m = _VERSION.match(str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def _get(url: str, opener, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with opener(req, timeout=30) as r:
        body = r.read(limit + 1)
    if len(body) > limit:
        raise ValueError("download too large")
    return body


def latest_release(opener=urllib.request.urlopen) -> dict:
    """The newest stable release: {version, wheel_name, wheel_url, sha256, page}."""
    rel = json.loads(_get(RELEASES_URL, opener, 1024 * 1024))
    if rel.get("draft") or rel.get("prerelease"):
        raise ValueError("latest release is not a stable release")
    tag = str(rel.get("tag_name") or "")
    ver = parse_version(tag)
    if not ver:
        raise ValueError(f"release tag {tag!r} is not a version")
    version = "%d.%d.%d" % ver
    wheel_name = f"{DIST}-{version}-py3-none-any.whl"
    assets = {a.get("name"): a for a in rel.get("assets") or []}
    wheel = assets.get(wheel_name)
    if not wheel:
        raise ValueError(f"release {tag} has no {wheel_name}")
    url = str(wheel.get("browser_download_url") or "")
    if not url.startswith(DOWNLOAD_PREFIX):
        raise ValueError("wheel is not hosted in this project's releases")
    sha = str(wheel.get("digest") or "").removeprefix("sha256:").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        # Older GitHub responses carry no digest: the release's catalogue-entry.json records the wheel's SHA-256.
        entry = assets.get("catalogue-entry.json") or {}
        eurl = str(entry.get("browser_download_url") or "")
        if not eurl.startswith(DOWNLOAD_PREFIX):
            raise ValueError("release has no checksum for the wheel")
        doc = json.loads(_get(eurl, opener, 64 * 1024))
        if doc.get("version") != version:
            raise ValueError("checksum file is for another version")
        sha = str(doc.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("release has no checksum for the wheel")
    return {"version": version, "wheel_name": wheel_name, "wheel_url": url, "sha256": sha,
            "page": str(rel.get("html_url") or f"https://github.com/{REPO}/releases")}


def download(info: dict, dest: Path, opener=urllib.request.urlopen) -> Path:
    """Fetch the wheel into dest/, keep it only if its SHA-256 matches, and drop older downloads."""
    dest.mkdir(parents=True, exist_ok=True)
    body = _get(info["wheel_url"], opener, MAX_WHEEL)
    got = hashlib.sha256(body).hexdigest()
    if got != info["sha256"]:
        raise ValueError(f"checksum mismatch (got {got[:12]}..., expected {info['sha256'][:12]}...)")
    path = dest / info["wheel_name"]
    tmp = path.with_suffix(".part")
    tmp.write_bytes(body)
    tmp.replace(path)
    for old in dest.glob("*.whl"):
        if old != path:
            old.unlink(missing_ok=True)
    return path


class Updater:
    """State for the page: {current, latest, available, checked_at, error, wheel_path, ready, busy, done_request}."""

    def __init__(self, data_dir: Path, opener=urllib.request.urlopen):
        self.dir = data_dir / "update"
        self.opener = opener
        self.info: dict | None = None
        self.s: dict = {"current": __version__, "latest": None, "available": False, "checked_at": None,
                        "error": None, "wheel_path": None, "ready": None, "busy": False, "done_request": None,
                        "page": None}
        self.request_id: str | None = None
        self.changed = False

    def view(self) -> dict:
        return dict(self.s)

    def _check(self) -> None:
        self.info = latest_release(self.opener)
        self.s.update(latest=self.info["version"], available=newer(self.info["version"], __version__),
                      page=self.info["page"], error=None)

    def _work(self, action: str, rid: str | None) -> None:
        try:
            self._check()
            if action == "download" and self.s["available"]:
                path = download(self.info, self.dir, self.opener)
                self.s.update(wheel_path=str(path.resolve()), ready=self.info["version"])
                log.info("update %s downloaded and verified: %s", self.info["version"], path)
        except Exception as e:  # network or GitHub trouble: report it, never stop the plugin
            self.s["error"] = str(e)[:200]
            log.warning("update %s failed: %s", action, e)
        finally:
            self.s["checked_at"] = time.time()
            self.s["busy"] = False
            if rid:
                self.s["done_request"] = rid
            self.changed = True

    def tick(self, request: dict | None) -> None:
        """Called every poll: run a page request ({id, action: check|download}) or the daily check."""
        if self.s["busy"]:
            return
        req = request or {}
        rid = str(req.get("id") or "")
        if rid and rid != self.request_id:
            self.request_id = rid
            action = "download" if req.get("action") == "download" else "check"
        elif time.time() - (self.s["checked_at"] or 0) >= CHECK_EVERY:
            rid, action = None, "check"
        else:
            return
        self.s["busy"] = True
        self.changed = True
        threading.Thread(target=self._work, args=(action, rid or None), name="update", daemon=True).start()
