"""Build Porchlight's shared names catalogue: the one place every porch gets channel names from.

Output (OUT_DIR, published by .github/workflows/names.yml at a permanent address):
  index.json       {"schema": 1, "updated", "parts": [{"name", "path", "sha256", "bytes", "names"}], "report"}
  real.txt.gz      names people really use: the community MeshCore channel catalog plus the channel names public
                   CoreScope instances have seen (tools/corescopes.json). Small; changes most days.
  guesses.txt.gz   the extended guess list from tools/build_names.py. Big; rebuilt only now and then.

A porch reads index.json daily and downloads only a part whose SHA-256 changed (porchlight/sources.py, type "index").

Names are only ever added. The new real part is yesterday's real part plus today's names, so a CoreScope that is down
or a channel that went quiet never removes a name. A guess list much smaller than the published one is refused, and
without --guesses the published guess list is carried over byte for byte (same hash: no porch downloads it again).

    python tools/build_index.py OUT_DIR [--previous URL] [--guesses guesses.txt.gz]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from build_names import packed  # noqa: E402
from porchlight.sources import UA, _parse_list, clean  # noqa: E402

CATALOG = "https://raw.githubusercontent.com/marcelverdult/meshcore-channels/main/channels-unique.json"
GUESS_FLOOR = 0.9  # a new guess list below 90% of the published one is refused


def get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(64 * 1024 * 1024)


def corescope_names(base: str) -> set[str]:
    """Hashtag channel names a CoreScope has seen. Only names that already start with '#': a CoreScope also lists
    labels for channels it can't name, and adding a '#' to those would point at a different channel."""
    base = base.rstrip("/")
    names = [c.get("name") for c in json.loads(get(base + "/api/channels")).get("channels", [])]
    try:
        a = json.loads(get(base + "/api/analytics/channels"))
    except Exception:
        a = {}  # not every instance has the analytics endpoint
    names += [c.get("name") for c in a.get("channels", [])] + [x.get("channel") for x in a.get("channelTimeline", [])]
    return clean(n for n in names if str(n or "").strip().startswith("#"))


def previous(url: str | None, first_run: bool = False) -> dict:
    """The published catalogue: {"index": {...}, "<part name>": bytes}. Missing is only accepted on a first run: a
    catalogue that is suddenly not there (Pages off, a wrong address) must not restart from today's names alone."""
    if not url:
        return {}
    base = url.rstrip("/") + "/"
    try:
        idx = json.loads(get(base + "index.json"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and first_run:
            return {}
        if e.code == 404:
            raise SystemExit(f"no published catalogue at {base}; run with --first-run only if this is the first one")
        raise
    out = {"index": idx}
    for part in idx.get("parts", []):
        raw = get(urllib.parse.urljoin(base, part["path"]), timeout=120)
        if hashlib.sha256(raw).hexdigest() != part["sha256"]:
            raise SystemExit(f"published part {part['name']} doesn't match its SHA-256; not building on it")
        out[part["name"]] = raw
    return out


def part(name: str, path: str, raw: bytes) -> dict:
    return {"name": name, "path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "names": len(clean(_parse_list(raw, path)))}


def build(out: Path, prev: dict, guesses: bytes | None, catalog_url: str, corescopes: list[dict]) -> dict:
    report = {}
    real = clean(_parse_list(prev["real"], "real.txt.gz")) if "real" in prev else set()
    before = len(real)
    try:
        real |= clean(e.get("channel") for e in json.loads(get(catalog_url)).get("channels", []))
        report["MeshCore channel catalog"] = "ok"
    except Exception as e:
        report["MeshCore channel catalog"] = f"failed: {e}"
    for cs in corescopes:
        try:
            got = corescope_names(cs["url"])
            real |= got
            report[cs["name"]] = f"ok, {len(got)} names"
        except Exception as e:
            report[cs["name"]] = f"failed: {str(e)[:120]}"
    if not real:
        raise SystemExit("no real names at all (every source failed and nothing was published before)")
    real_raw = packed("\n".join(["// Porchlight names: channel names people really use (tools/build_index.py)"]
                                + sorted(real)) + "\n")

    if guesses is None:
        if "guesses" not in prev:
            raise SystemExit("no guess list: pass --guesses (the first run, or the published one is missing)")
        guesses = prev["guesses"]  # carried over unchanged: same bytes, same hash
    elif "guesses" in prev:
        new_n = len(clean(_parse_list(guesses, "guesses.txt.gz")))
        old_n = len(clean(_parse_list(prev["guesses"], "guesses.txt.gz")))
        if new_n < old_n * GUESS_FLOOR:
            raise SystemExit(f"new guess list has {new_n} names, the published one {old_n}: refused")

    out.mkdir(parents=True, exist_ok=True)
    (out / "real.txt.gz").write_bytes(real_raw)
    (out / "guesses.txt.gz").write_bytes(guesses)
    index = {"schema": 1, "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "parts": [part("real", "real.txt.gz", real_raw), part("guesses", "guesses.txt.gz", guesses)],
             "report": report}
    (out / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    print(f"real: {before} -> {len(real)} names; guesses: {index['parts'][1]['names']} names")
    for k, v in report.items():
        print(f"  {k}: {v}")
    return index


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--previous", help="base URL of the published catalogue (its index.json and parts)")
    ap.add_argument("--first-run", action="store_true", help="no catalogue is published yet: start one")
    ap.add_argument("--guesses", type=Path, help="a freshly built guess list (tools/build_names.py)")
    ap.add_argument("--catalog", default=CATALOG)
    ap.add_argument("--corescopes", type=Path, default=ROOT / "tools" / "corescopes.json")
    a = ap.parse_args()
    corescopes = json.loads(a.corescopes.read_text())["corescopes"]
    build(a.out, previous(a.previous, a.first_run), a.guesses.read_bytes() if a.guesses else None, a.catalog, corescopes)


if __name__ == "__main__":
    main()
