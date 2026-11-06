"""Build Porchlight's extended channel-name list: names to try against hashtag channels nobody has named yet.

A hashtag channel's key is sha256("#name")[:16], so its name is the only secret, and it is public by design. This list
is the guesses a porch tries; it never touches private channels (their keys are random).

Inputs (downloaded by hand, paths passed as arguments; see README "Extended name list"):
  en_US.dic        SCOWL / Hunspell en_US dictionary, github.com/en-wl/wordlist (SCOWL copyright notice, permissive)
  cities1000.txt   GeoNames places with population >= 1000, download.geonames.org/export/dump (CC BY 4.0)
  admin1CodesASCII.txt, admin2Codes.txt   GeoNames states/provinces and counties (CC BY 4.0)
  catalog.json     the community MeshCore channel catalog, github.com/marcelverdult/meshcore-channels (CC0)

Patterns follow what that catalog shows people actually use: plain words, place names, short numbers, a country-code
prefix ("de-berlin"), and suffixes like "mesh", "-wx", "-bot", "-test".

    python tools/build_names.py IN_DIR OUT.txt.gz [--max-bytes 2000000]
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import unicodedata
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")  # same rule as porchlight/sources.py, without the '#'

# Suffixes seen on place names in the catalog, joined and hyphenated.
SUFFIXES = ["mesh", "-mesh", "wx", "-wx", "-weather", "chat", "-chat", "net", "-net", "-ares", "ares", "-bot", "bot",
            "-test", "test", "-alerts", "-alert", "-emcomm", "-ham", "ham", "-radio", "-news", "-events", "-local",
            "-ops", "-sar", "-mc", "-meshcore", "-lora", "-help", "-ping"]
# Countries whose meshes use a "cc-" prefix in the catalog.
CC_PREFIX = {"de", "fr", "nl", "dk", "at", "be", "ch", "lt", "pl", "cz", "se", "no", "fi", "it", "es", "pt", "uk", "ie"}


def slug(s: str) -> tuple[str, str]:
    """('fortwayne', 'fort-wayne') for 'Fort Wayne'; ASCII only."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s.replace(" ", ""), s.replace(" ", "-")


def ok(n: str) -> bool:
    return bool(NAME_RE.match(n))


TOPIC_SUFFIXES = ["-gmrs", "gmrs", "-hams", "-skywarn", "-cert", "-preppers"]  # "#indy-gmrs", "#ohio-skywarn"
ROOT = Path(__file__).resolve().parents[1]


def plural(w: str) -> str:
    if w[-1].isdigit():
        return ""
    if w.endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    return w + "s"


def topics() -> list[str]:
    """tools/topics.txt (comma-separated; phrases give joined and hyphenated forms, every form a plural), then the
    plugin's own built-in word list."""
    out: list[str] = []
    for line in (ROOT / "tools" / "topics.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("//"):
            continue
        for entry in line.split(","):
            words = [w for w in re.split(r"[\s]+", entry.strip().lower()) if w]
            if not words:
                continue
            forms = ["".join(words), "-".join(words)] if len(words) > 1 else words
            for form in forms:
                out += [form, plural(form)]
    builtin = re.search(r'_GENERAL = """(.*?)"""', (ROOT / "porchlight" / "wordlist.py").read_text(), re.S)
    out += builtin.group(1).split() if builtin else []
    return [n for n in out if n]


TOP_SUFFIXES = ["mesh", "-mesh", "wx", "-wx", "-bot", "-test", "bot", "net", "-chat", "-ares"]  # the most used ones


def build(src: Path) -> list[tuple[str, list[str]]]:
    """Tiers of names, most likely first; the size cap trims from the end."""
    numbers = [f"{i:0{w}d}" for w in (1, 2, 3, 4) for i in range(10 ** w)]  # "#112", "#446", area codes

    words, plurals = set(), set()  # everyday English (SCOWL normal size), and plurals of the nouns (Hunspell flag S)
    for line in (src / "en_US.dic").read_text(encoding="utf-8", errors="ignore").splitlines()[1:]:
        stem, _, flags = line.strip().partition("/")
        w = stem.lower().replace("'", "")
        if 2 <= len(w) <= 20 and ok(w):
            words.add(w)
            if "S" in flags and ok(p := plural(w)):
                plurals.add(p)

    catalog = set()
    for e in json.loads((src / "catalog.json").read_text()).get("channels", []):
        n = str(e.get("channel") or "").lower().lstrip("#")
        if ok(n):
            catalog.add(n)

    # Places with population, biggest first. Countries with the most MeshCore users go down to 1000 people.
    big_cc = {"US", "CA", "GB", "IE", "AU", "NZ", "DE", "NL", "BE", "DK", "AT", "CH", "FR", "LT", "PL", "CZ", "SE", "NO",
              "FI"}
    places = []  # (pop, cc, joined, hyphenated)
    for line in (src / "cities1000.txt").read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        name, cc, pop = f[2], f[8], int(f[14] or 0)
        if pop >= (1000 if cc in big_cc else 5000):
            places.append((pop, cc, *slug(name)))
    places.sort(reverse=True)

    us_states, regions = [], []  # US state names + codes; other states/provinces and counties
    for fn in ("admin1CodesASCII.txt", "admin2Codes.txt"):
        for line in (src / fn).read_text(encoding="utf-8").splitlines():
            f = line.split("\t")
            code, name = f[0], f[2] if len(f) > 2 else f[1]
            joined, hyph = slug(re.sub(r" (County|Parish|Borough)$", "", name))
            if fn.startswith("admin1") and code.startswith("US."):
                us_states += [joined, hyph, code[3:].lower()]
            else:
                regions += [joined, hyph]

    def with_suffixes(bases, sufs):
        return [b + s for b in bases for s in sufs]

    big = lambda p, cc: p >= 250000 or (cc == "US" and p >= 25000) or (cc in big_cc and p >= 50000)
    medium = lambda p, cc: p >= 50000 or (cc == "US" and p >= 5000) or (cc in big_cc and p >= 10000)
    big_bases = us_states + [j for p, cc, j, _ in places if big(p, cc)]
    medium_bases = [j for p, cc, j, _ in places if medium(p, cc) and not big(p, cc)]
    prefixed = [f"{'uk' if cc == 'GB' else cc.lower()}-{j}" for p, cc, j, _ in places
                if (cc.lower() in CC_PREFIX or cc == "GB") and p >= 20000]
    return [
        ("catalog", sorted(catalog)),
        ("topics", topics()),
        ("numbers", numbers),
        ("words", sorted(words)),
        ("states", us_states),
        ("big places", [n for p, cc, j, h in places if big(p, cc) for n in (j, h)]),
        ("big places + top suffixes", with_suffixes(big_bases, TOP_SUFFIXES)),
        ("medium places", [n for p, cc, j, h in places if medium(p, cc) and not big(p, cc) for n in (j, h)]),
        ("plurals", sorted(plurals)),
        ("big places + topic suffixes", with_suffixes(big_bases, TOPIC_SUFFIXES)),
        ("other places", [n for p, cc, j, h in places if not medium(p, cc) for n in (j, h)]),
        ("country-prefixed places", prefixed),
        ("medium places + top suffixes", with_suffixes(medium_bases, TOP_SUFFIXES)),
        ("big places + other suffixes", with_suffixes(big_bases, [x for x in SUFFIXES if x not in TOP_SUFFIXES])),
        ("states, provinces, counties", regions),
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("inputs", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--max-bytes", type=int, default=2_000_000, help="cap on the unpacked list size")
    a = ap.parse_args()
    tiers = build(a.inputs)
    names: list[str] = []
    seen: set[str] = set()
    size, report, full = 0, [], False
    for label, tier in tiers:  # most likely first; the cap trims the least likely
        added = 0
        for n in tier:
            if n in seen or not ok(n):
                continue
            if size + len(n) + 2 > a.max_bytes:
                full = True
                break
            seen.add(n)
            names.append(n)
            size += len(n) + 2
            added += 1
        report.append(f"{label}: {added}")
        if full:
            break
    header = [
        "// Porchlight extended channel-name list: names a porch tries against unnamed hashtag channels.",
        "// Built by tools/build_names.py from: SCOWL en_US (en-wl/wordlist), GeoNames (CC BY 4.0, geonames.org),",
        "// the MeshCore channel catalog (CC0, github.com/marcelverdult/meshcore-channels).",
        f"// {len(names)} names",
    ]
    body = "\n".join(header + ["#" + n for n in sorted(names)]) + "\n"
    with gzip.open(a.out, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(body)
    print(f"{len(names)} names, {len(body.encode())} bytes unpacked, {a.out.stat().st_size} bytes gzipped")
    print("  " + "\n  ".join(report))


if __name__ == "__main__":
    main()
