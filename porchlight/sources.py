"""Name sources: public lists of hashtag channel names, fetched now and then and tried as discovery guesses.

A source is {"name", "type", "url", "every_hours"} in config `name_sources`:
- "index": Porchlight's shared names catalogue, rebuilt daily (tools/build_index.py). The URL is a small index.json
  listing its parts with their SHA-256; only a part whose hash changed is downloaded, and it is kept only if its bytes
  match that hash. Names from an index are only ever added: one that later drops out of the catalogue is still tried.
- "catalog": a JSON file shaped like the community MeshCore channel catalog,
  {"channels": [{"channel": "#name", ...}, ...]} (github.com/marcelverdult/meshcore-channels, CC0)
- "list": a plain-text file, one name per line ('#' optional, lines starting "//" are comments), gzipped if the URL
  ends in .gz. Porchlight's own extended name list is one.
Only '#' names are kept: a hashtag key is public by design, a private channel's name is never used.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import time
import urllib.parse
import urllib.request

NAME_RE = re.compile(r"^#[a-z0-9][a-z0-9_-]{0,40}$")
UA = "porchlight-openhop-plugin (+https://github.com/mutiny19/porchlight-openhop-plugin)"


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


MAX_LIST_BYTES = 32 * 1024 * 1024  # a name list larger than this (unpacked) is refused


def _get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read(MAX_LIST_BYTES + 1)
    if len(raw) > MAX_LIST_BYTES:
        raise ValueError("name list too large")
    return raw


def _get_list(url: str) -> list[str]:
    return _parse_list(_get_bytes(url), url)


def _parse_list(raw: bytes, url: str = "") -> list[str]:
    if url.split("?", 1)[0].endswith(".gz") or raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as g:
            raw = g.read(MAX_LIST_BYTES + 1)
    if len(raw) > MAX_LIST_BYTES:
        raise ValueError("name list too large")
    return [ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip() and not ln.startswith("//")]


def clean(names) -> set[str]:
    """Normalised '#name's."""
    out = set()
    for n in names:
        n = "#" + str(n or "").strip().lstrip("#").lower()
        if NAME_RE.match(n):
            out.add(n)
    return out


def fetch(src: dict) -> set[str]:
    kind, url = src.get("type"), str(src.get("url") or "").rstrip("/")
    if kind == "catalog":
        return clean(e.get("channel") for e in _get(url).get("channels", []))
    if kind == "corescope":
        raise ValueError("CoreScope sources are no longer supported: remove this one")
    if kind == "list":
        return clean(_get_list(url))
    raise ValueError(f"unknown source type {kind!r}")


def fetch_index(src: dict, prev: dict) -> tuple[set[str], dict]:
    """(names, {part name: sha256}) for an "index" source. prev is this source's cache entry: parts whose hash is
    unchanged aren't downloaded again, and its names are kept (an index only adds). Raises, keeping prev as it was,
    when the index is malformed or a part doesn't match its hash."""
    url = str(src.get("url") or "")
    idx = _get(url)
    parts = idx.get("parts") if isinstance(idx, dict) else None
    if idx.get("schema") != 1 or not isinstance(parts, list) or not parts:
        raise ValueError("not a Porchlight names index")
    have = dict(prev.get("parts") or {})
    names = set(prev.get("names") or [])
    for part in parts:
        name, sha, path = part.get("name"), str(part.get("sha256") or "").lower(), part.get("path")
        if not name or not path or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("bad part in the names index")
        if have.get(name) == sha:
            continue  # unchanged since the last download
        raw = _get_bytes(urllib.parse.urljoin(url, path))
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError(f"part {name!r} doesn't match its SHA-256 (incomplete or changed download)")
        got = clean(_parse_list(raw, path))
        if isinstance(part.get("names"), int) and len(got) < part["names"] * 0.9:
            raise ValueError(f"part {name!r} has {len(got)} names, the index says {part['names']}")
        names |= got
        have[name] = sha
    return names, have


def due(src: dict, cache: dict, now: float) -> bool:
    c = cache.get(src.get("url"), {})
    every = float(src.get("every_hours", 24)) * 3600
    # After a failure, retry in an hour rather than waiting a whole interval.
    last = c.get("fetched_at", 0) if not c.get("error") else c.get("tried_at", 0) - every + 3600
    return now - last >= every
