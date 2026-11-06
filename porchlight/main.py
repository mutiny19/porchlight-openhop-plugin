"""Porchlight backend: polls openHop's packet log, decrypts what it can, writes data/runtime.json for the UI, and (once
its owner lights the porch) answers in #porchlight through its own companion (porch.py, speaker.py)."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import sources
from .core import Store
from .porch import CHANNEL, Api, Porch
from .regions import Regions
from .speaker import Speaker

log = logging.getLogger("porchlight")
GRP_TXT = 5
PAGE = 1000


class Watcher:
    def __init__(self, data_dir: Path):
        self.data = data_dir
        self.cfg_path = data_dir / "config.json"
        self.state_path = data_dir / "state.json"
        self.runtime_path = data_dir / "runtime.json"
        self.sources_path = data_dir / "sources.json"
        self.sources_cache: dict = {}  # url -> {name, names, fetched_at, tried_at, error}
        self._fetching = False
        self.cfg: dict = {}
        self.cfg_mtime = None
        self.store = Store()
        self.cursor = None  # newest packet timestamp already fetched
        self.status = "starting"
        self.last_status = None
        self.last_state_write = 0.0
        self.state_dirty = False
        self.last_runtime_write = 0.0
        self.running = True
        self.sources_changed = False
        self.regions = Regions(data_dir / "regions.json")  # region scopes heard
        self.results: list[dict] = []  # latest inbox results (names dropped in data/inbox by other tools)
        self.latest: dict | None = None  # the last runtime.json written; the speaker answers from it
        self.porch = Porch(data_dir)
        self.speaker = Speaker(data_dir, self.porch, self.store, lambda: self.latest)
        self._lighting = False

    # ---- files ----
    @staticmethod
    def _atomic_write(path: Path, obj) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, separators=(",", ":")))
        os.replace(tmp, path)

    def _token(self) -> str:
        # Settings field first (like Prometheus's repeater_api_token), then a data/api_token file.
        tok = str(self.cfg.get("api_token") or "").strip() or os.environ.get("PORCHLIGHT_API_TOKEN", "")
        if not tok:
            try:
                tok = (self.data / "api_token").read_text().strip()
            except OSError:
                tok = ""
        return tok

    def reload_config(self) -> bool:
        try:
            mtime = self.cfg_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self.cfg_mtime and self.cfg:
            return False
        self.cfg_mtime = mtime
        try:
            self.cfg = json.loads(self.cfg_path.read_text())
        except (OSError, ValueError):
            self.cfg = {}
        self.store.configure(self.cfg)
        base = self.cfg.get("api_base", "http://127.0.0.1:8000")
        self.porch.configure(self.cfg, Api(base, self._token()) if self._token() else None)
        self.speaker.configure(self.cfg)
        log.info("config loaded: %d hashtags, %d private, discovery=%s, %d guesses",
                 len(self.cfg.get("hashtags", [])), len(self.cfg.get("private", [])),
                 self.cfg.get("discovery", True), len(self.store.guesses))
        return True

    def load_state(self) -> None:
        try:
            d = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.store.load_state(d)
        self.cursor = d.get("cursor")
        log.info("state restored: %d channels, cursor=%s", len(self.store.channels), self.cursor)
        try:
            self.sources_cache = json.loads(self.sources_path.read_text())
        except (OSError, ValueError):
            self.sources_cache = {}

    def save(self, force: bool = False) -> None:
        now = time.time()
        # At most one page-data write per 10 s, however busy the channels get (SD card).
        if force or (self.store.dirty and now - self.last_runtime_write >= 10):
            self.last_runtime_write = now
            view = {**self.store.view(now), "status": self.status, "per_channel": self.store.per_channel,
                    "sources": self._sources_view(), "submissions": self.results, "regions": self.regions.view(),
                    "porch": self.porch.view()}
            self._atomic_write(self.runtime_path, view)
            self.latest = view
            self.store.dirty = False
            self.state_dirty = True
        # The full state (incl. undecoded payloads) is bigger: write it only after changes, at most once a minute (SD card).
        if self.state_dirty and (force or now - self.last_state_write > 60):
            self._atomic_write(self.state_path, {**self.store.to_state(), "cursor": self.cursor})
            self.last_state_write = now
            self.state_dirty = False

    # ---- names sent in ----
    def process_inbox(self) -> None:
        """`porchlight add #name` from another program (such as a bot): it drops one JSON file per request ({id, t, by, names}) in the inbox
        (config inbox_dir, default data/inbox); each name is tried at once and the result goes into runtime.json."""
        inbox = Path(self.cfg.get("inbox_dir") or self.data / "inbox")
        try:
            files = sorted(inbox.glob("*.json"))[:20]
        except OSError:
            return
        done = False
        for f in files:
            try:
                req = json.loads(f.read_text())
            except (OSError, ValueError):
                req = None
            try:
                f.unlink()
            except OSError:
                pass
            if not isinstance(req, dict) or not isinstance(req.get("names"), list):
                continue
            t = float(req.get("t") or time.time())
            results = [self.store.submit(n, t) for n in req["names"][:5]]
            self.results = (self.results + [{"id": str(req.get("id") or f.stem), "t": time.time(), "results": results}])[-50:]
            log.info("names sent in by %s: %s", str(req.get("by") or "?")[:40],
                     ", ".join(f"{r['name']} {r['result']}" for r in results))
            done = True
        if done:
            self.store.dirty = True
            self.save(force=True)  # the bot is waiting for the answer

    # ---- name sources ----
    def _sources_view(self) -> list[dict]:
        out = []
        for src in self.cfg.get("name_sources", []):
            c = self.sources_cache.get(src.get("url"), {})
            out.append({"name": src.get("name") or src.get("url"), "url": src.get("url"),
                        "count": len(c.get("names", [])), "fetched_at": c.get("fetched_at"), "error": c.get("error")})
        return out

    def apply_sources(self) -> None:
        urls = {s.get("url") for s in self.cfg.get("name_sources", [])}
        names = set()
        for url, c in self.sources_cache.items():
            if url in urls:
                names |= set(c.get("names", []))
        before = len(self.store.guesses)
        self.store.set_source_names(names)
        if len(self.store.guesses) != before:
            log.info("name sources: %d names, %d guesses now", len(names), len(self.store.guesses))
            self.store.dirty = True

    def refresh_sources(self) -> None:
        """Fetch any source that is due, in a thread so a slow site never stalls packet polling."""
        if self._fetching:
            return
        now = time.time()
        todo = [s for s in self.cfg.get("name_sources", []) if s.get("url") and sources.due(s, self.sources_cache, now)]
        if not todo:
            return
        self._fetching = True

        def work():
            try:
                for src in todo:
                    entry = dict(self.sources_cache.get(src["url"], {}), name=src.get("name"), tried_at=time.time())
                    try:
                        entry["names"] = sorted(sources.fetch(src))
                        entry["fetched_at"], entry["error"] = time.time(), None
                        log.info("name source %s: %d names", src.get("name") or src["url"], len(entry["names"]))
                    except Exception as e:
                        entry["error"] = str(e)[:200]
                        log.warning("name source %s failed: %s", src.get("name") or src["url"], e)
                    self.sources_cache[src["url"]] = entry
                self._atomic_write(self.sources_path, self.sources_cache)
                self.sources_changed = True
            finally:
                self._fetching = False

        threading.Thread(target=work, name="name-sources", daemon=True).start()

    # ---- openHop API ----
    def fetch(self, start: float, end: float | None = None) -> list[dict]:
        q = {"type": GRP_TXT, "start_timestamp": f"{start:.3f}", "limit": PAGE}
        if end is not None:
            q["end_timestamp"] = f"{end:.3f}"
        base = self.cfg.get("api_base", "http://127.0.0.1:8000").rstrip("/")
        req = urllib.request.Request(f"{base}/api/filtered_packets?{urllib.parse.urlencode(q)}",
                                     headers={"X-API-Key": self._token()})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.load(r)
        if not body.get("success", True):
            raise RuntimeError(body.get("error", "API error"))
        return body.get("data", [])

    def fetch_since(self, start: float) -> list[dict]:
        """Newest-first pages walked backwards until the window is covered."""
        out, end = [], None
        while True:
            page = self.fetch(start, end)
            out.extend(page)
            if len(page) < PAGE:
                break
            end = min(p["timestamp"] for p in page)
            if end <= start:
                break
        return out

    def poll(self) -> None:
        now = time.time()
        start = self.cursor - 30 if self.cursor else now - float(self.cfg.get("backfill_hours", 48)) * 3600
        recs = self.fetch_since(start)
        recs.sort(key=lambda p: (p.get("timestamp") or 0, p.get("id") or 0))
        for rec in recs:
            new = self.store.add(rec)
            if new and new[0] == CHANNEL:
                self.speaker.hear(new[1])
        if recs:
            self.cursor = max(self.cursor or 0, recs[-1]["timestamp"])
        elif self.cursor is None:
            self.cursor = now
        self.store.prune(now)
        self.speaker.consider_news()

    # ---- the porch ----
    def porch_requests(self) -> None:
        """'Light the porch' / 'Turn off' / 'Remove' from the page: it writes light_request {id, action} into the
        settings."""
        req = self.cfg.get("light_request") or {}
        rid = str(req.get("id") or "")
        if not rid or rid == self.porch.s.get("request_id") or self._lighting:
            if self.porch.lit and time.time() - self.porch.last_check > 300:
                was = self.porch.lit
                self.porch.check()  # someone may have changed the channel slot: go dark if so
                if was != self.porch.lit:
                    self.store.dirty = True
            return
        self.porch.s["request_id"] = rid
        if req.get("action") == "off":
            self.porch.go_dark("Turned off by its owner")
            self.porch.s["done_request"] = rid
            self.store.dirty = True
            return
        if req.get("action") == "remove":
            self.porch.remove()
            self.porch.s["done_request"] = rid  # the page revokes its API token only after this
            self.store.dirty = True
            return
        self._lighting = True
        self.porch.s.update(reason="Lighting the porch...", steps=[])
        self.store.dirty = True

        def work():
            try:
                self.porch.light()
            finally:
                self.porch.s["done_request"] = rid
                self._lighting = False
                self.store.dirty = True

        threading.Thread(target=work, name="light-porch", daemon=True).start()

    def run(self) -> None:
        self.load_state()
        self.reload_config()
        self.apply_sources()
        while self.running:
            if self.reload_config():
                self.apply_sources()  # configure() rebuilt the guesses
            self.refresh_sources()
            if self.sources_changed:
                self.sources_changed = False
                self.apply_sources()
            try:
                if not self._token():
                    self.status = "no openHop API token yet: press Connect on the Porchlight page"
                else:
                    self.poll()
                    self.status = "ok"
                    try:
                        base = self.cfg.get("api_base", "http://127.0.0.1:8000").rstrip("/")
                        if self.regions.update(base, self._token()):
                            self.store.dirty = True
                    except Exception as e:  # regions are extra: never stop channel polling over them
                        log.warning("regions update failed: %s", e)
                self.process_inbox()
                self.porch_requests()
            except urllib.error.HTTPError as e:
                self.status = f"openHop API {e.code}" + (": check the API token (press Connect again on the Porchlight page)" if e.code in (401, 403) else "")
            except Exception as e:  # keep running; openHop restarts are normal
                self.status = f"error: {e}"
                log.warning("poll failed: %s", e)
            try:
                if self.status != self.last_status:  # show status changes without rewriting on every poll
                    self.store.dirty, self.last_status = True, self.status
                self.save()
            except OSError as e:
                log.warning("write failed: %s", e)
            for _ in range(int(self.cfg.get("poll_seconds", 5) * 10)):
                if not self.running:
                    break
                time.sleep(0.1)
        self.save(force=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    data = Path(os.environ.get("OPENHOP_PLUGIN_DATA", "."))
    data.mkdir(parents=True, exist_ok=True)
    w = Watcher(data)

    def stop(*_):
        w.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("Porchlight starting, data=%s", data)
    w.run()


if __name__ == "__main__":
    main()
