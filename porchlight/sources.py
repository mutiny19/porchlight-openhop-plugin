"""Name sources: public lists of hashtag channel names, fetched now and then and tried as discovery guesses.

A source is {"name", "type", "url", "every_hours"} in config `name_sources`:
- "catalog": a JSON file shaped like the community MeshCore channel catalog,
  {"channels": [{"channel": "#name", ...}, ...]} (github.com/marcelverdult/meshcore-channels, CC0)
- "corescope": a CoreScope instance's base URL; names come from /api/channels and /api/analytics/channels
- "list": a plain-text file, one name per line ('#' optional, lines starting "//" are comments), gzipped if the URL
  ends in .gz. Porchlight's own extended name list is one.
Only '#' names are kept: a hashtag key is public by design, a private channel's name is never used.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import time
import urllib.request

NAME_RE = re.compile(r"^#[a-z0-9][a-z0-9_-]{0,40}$")
UA = "porchlight-openhop-plugin (+https://github.com/mutiny19/porchlight-openhop-plugin)"


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


MAX_LIST_BYTES = 32 * 1024 * 1024  # a name list larger than this (unpacked) is refused


def _get_list(url: str) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read(MAX_LIST_BYTES + 1)
    if url.split("?", 1)[0].endswith(".gz") or raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as g:
            raw = g.read(MAX_LIST_BYTES + 1)
    if len(raw) > MAX_LIST_BYTES:
        raise ValueError("name list too large")
    return [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip() and not ln.startswith("//")]


def clean(names, require_hash: bool = False) -> set[str]:
    """Normalised '#name's. require_hash: drop names without a leading '#' (a CoreScope list can include
    non-hashtag channel names and labels for unnamed channels; adding a '#' to those would point at a different channel)."""
    out = set()
    for n in names:
        n = str(n or "").strip()
        if require_hash and not n.startswith("#"):
            continue
        n = "#" + n.lstrip("#").lower()
        if NAME_RE.match(n):
            out.add(n)
    return out


def fetch(src: dict) -> set[str]:
    kind, url = src.get("type"), str(src.get("url") or "").rstrip("/")
    if kind == "catalog":
        return clean(e.get("channel") for e in _get(url).get("channels", []))
    if kind == "corescope":
        names = [c.get("name") for c in _get(url + "/api/channels").get("channels", [])]
        try:
            a = _get(url + "/api/analytics/channels")
        except Exception:
            a = {}  # not every instance offers the analytics endpoint; /api/channels alone is enough
        names += [c.get("name") for c in a.get("channels", [])]
        names += [x.get("channel") for x in a.get("channelTimeline", [])]
        return clean(names, require_hash=True)
    if kind == "list":
        return clean(_get_list(url))
    raise ValueError(f"unknown source type {kind!r}")


def due(src: dict, cache: dict, now: float) -> bool:
    c = cache.get(src.get("url"), {})
    every = float(src.get("every_hours", 24)) * 3600
    # After a failure, retry in an hour rather than waiting a whole interval.
    last = c.get("fetched_at", 0) if not c.get("error") else c.get("tried_at", 0) - every + 3600
    return now - last >= every
