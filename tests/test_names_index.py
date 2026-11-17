"""The shared names catalogue (index.json + parts): porches download only changed parts, check every part against its
SHA-256, and only ever add names. A name in the catalogue is only something to try: a channel exists, and counts as
new, only once this porch has decoded traffic on it. No network: downloads are faked."""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import sys
import time
from pathlib import Path

import pytest
from Crypto.Cipher import AES
import hmac

from porchlight import sources
from porchlight import speaker as sp
from porchlight.core import Store, channel_hash, hashtag_secret

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_index  # noqa: E402
import build_names  # noqa: E402

INDEX = "https://example.org/names/index.json"


def gz(names):
    return build_names.packed("\n".join(sorted(names)) + "\n")


class Net:
    """Fake web: url -> bytes, and a log of what was downloaded."""

    def __init__(self, files):
        self.files, self.got = dict(files), []

    def bytes(self, url):
        self.got.append(url)
        return self.files[url]

    def json(self, url):
        self.got.append(url)
        return json.loads(self.files[url])


def catalogue(parts: dict) -> dict:
    """{part name: names} -> fake files for an index and its parts."""
    files, entries = {}, []
    for name, names in parts.items():
        raw = gz(names)
        files[f"https://example.org/names/{name}.txt.gz"] = raw
        entries.append({"name": name, "path": f"{name}.txt.gz", "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw), "names": len(names)})
    files[INDEX] = json.dumps({"schema": 1, "parts": entries}).encode()
    return files


@pytest.fixture
def net(monkeypatch):
    n = Net({})
    monkeypatch.setattr(sources, "_get", n.json)
    monkeypatch.setattr(sources, "_get_bytes", n.bytes)
    return n


SRC = {"name": "Porchlight names", "type": "index", "url": INDEX}


def test_only_changed_parts_are_downloaded(net):
    net.files = catalogue({"real": ["#aa", "#bb"], "guesses": ["#cc"]})
    names, parts = sources.fetch_index(SRC, {})
    assert names == {"#aa", "#bb", "#cc"}
    net.got.clear()
    net.files.update(catalogue({"real": ["#aa", "#bb", "#dd"], "guesses": ["#cc"]}))
    names, parts = sources.fetch_index(SRC, {"names": sorted(names), "parts": parts})
    assert names == {"#aa", "#bb", "#cc", "#dd"}
    assert net.got == [INDEX, "https://example.org/names/real.txt.gz"]  # guesses unchanged: not downloaded


def test_names_are_only_ever_added(net):
    net.files = catalogue({"real": ["#aa", "#bb"], "guesses": ["#cc"]})
    names, parts = sources.fetch_index(SRC, {})
    net.files = catalogue({"real": ["#bb"], "guesses": ["#cc"]})  # #aa dropped out of the catalogue
    names, _ = sources.fetch_index(SRC, {"names": sorted(names), "parts": parts})
    assert "#aa" in names


def test_a_part_that_does_not_match_its_hash_is_refused(net):
    net.files = catalogue({"real": ["#aa"], "guesses": ["#cc"]})
    net.files["https://example.org/names/real.txt.gz"] = gz(["#aa"])[:-5]  # cut short
    prev = {"names": ["#old"], "parts": {}}
    with pytest.raises(ValueError, match="SHA-256"):
        sources.fetch_index(SRC, prev)
    assert prev == {"names": ["#old"], "parts": {}}  # nothing changed


def test_a_part_far_smaller_than_the_index_says_is_refused(net):
    files = catalogue({"real": ["#aa"], "guesses": ["#cc"]})
    idx = json.loads(files[INDEX])
    idx["parts"][0]["names"] = 1000  # the index promises 1000 names, the part has 1
    files[INDEX] = json.dumps(idx).encode()
    net.files = files
    with pytest.raises(ValueError, match="the index says"):
        sources.fetch_index(SRC, {})


def test_something_that_is_not_an_index_is_refused(net):
    net.files = {INDEX: b'{"channels": []}'}
    with pytest.raises(ValueError, match="not a Porchlight names index"):
        sources.fetch_index(SRC, {})


# ---- a name in the catalogue is not a channel heard here ----

def packet(name, text, pid):
    secret = hashtag_secret(name)
    pt = struct.pack("<IB", int(time.time()), 0) + text.encode()
    pt += b"\0" * (-len(pt) % 16)
    ct = AES.new(secret, AES.MODE_ECB).encrypt(pt)
    mac = hmac.new(secret + bytes(16), ct, hashlib.sha256).digest()[:2]
    return {"id": pid, "timestamp": time.time(), "payload": (bytes([channel_hash(secret)]) + mac + ct).hex(),
            "packet_hash": f"P{pid}"}


def test_catalogue_names_alone_create_no_channels_and_nothing_new():
    st = Store()
    st.configure({})
    st.set_source_names({"#tn-blount", "#nwburbs", "#zzz-never-heard"})
    assert not any(k in st.channels for k in ("#tn-blount", "#nwburbs", "#zzz-never-heard"))
    view = st.view(time.time())
    assert not [c for c in view["channels"] if c["kind"] == "discovered"]


def test_a_catalogue_name_is_new_when_this_porch_first_hears_it(tmp_path):
    st = Store()
    st.configure({})
    st.set_source_names({"#tn-blount"})
    before = time.time()
    st.add(packet("#tn-blount", "Ann: hello", 1))
    c = st.channels["#tn-blount"]
    assert c.kind == "discovered" and c.found_at >= before  # found = first decoded here
    view = st.view(time.time())

    class P:
        area = ""

    s = sp.Speaker(tmp_path, P(), st, lambda: view)
    assert "#tn-blount" in s.newest_text(4)


def test_a_discovered_channel_survives_a_list_without_its_name():
    st = Store()
    st.configure({})
    st.set_source_names({"#tn-blount"})
    st.add(packet("#tn-blount", "Ann: hello", 1))
    st.set_source_names(set())  # the source was removed
    assert st.channels["#tn-blount"].kind == "discovered"


# ---- the daily build (tools/build_index.py) ----

def fake_get(monkeypatch, files):
    monkeypatch.setattr(build_index, "get", lambda url, timeout=30: files[url])


CS = [{"name": "Somewhere", "url": "https://cs.example"}]


def test_build_adds_to_yesterdays_names_and_keeps_the_guess_list(tmp_path, monkeypatch):
    files = {"https://cat.example/c.json": json.dumps({"channels": [{"channel": "#cat1"}]}).encode(),
             "https://cs.example/api/channels": json.dumps({"channels": [{"name": "#cs1"}, {"name": "Unnamed 4A"}]}).encode(),
             "https://cs.example/api/analytics/channels": b"{}"}
    fake_get(monkeypatch, files)
    prev = {"real": gz(["#yesterday"]), "guesses": gz(["#g1", "#g2"])}
    idx = build_index.build(tmp_path, prev, None, "https://cat.example/c.json", CS)
    real = sources.clean(sources._parse_list((tmp_path / "real.txt.gz").read_bytes(), "real.txt.gz"))
    assert real == {"#yesterday", "#cat1", "#cs1"}  # labels without '#' are not names
    assert (tmp_path / "guesses.txt.gz").read_bytes() == prev["guesses"]  # same bytes, same hash
    assert idx["parts"][1]["sha256"] == hashlib.sha256(prev["guesses"]).hexdigest()


def test_build_keeps_names_when_every_source_is_down(tmp_path, monkeypatch):
    def down(url, timeout=30):
        raise OSError("down")
    monkeypatch.setattr(build_index, "get", down)
    prev = {"real": gz(["#a", "#b"]), "guesses": gz(["#g"])}
    idx = build_index.build(tmp_path, prev, None, "https://cat.example/c.json", CS)
    assert idx["parts"][0]["names"] == 2 and "failed" in idx["report"]["Somewhere"]


def test_build_refuses_a_much_smaller_guess_list(tmp_path, monkeypatch):
    fake_get(monkeypatch, {"https://cat.example/c.json": b'{"channels": [{"channel": "#c"}]}'})
    prev = {"real": gz(["#a"]), "guesses": gz([f"#g{i}" for i in range(100)])}
    with pytest.raises(SystemExit, match="refused"):
        build_index.build(tmp_path, prev, gz(["#g1"]), "https://cat.example/c.json", [])


def test_same_names_give_the_same_bytes():
    assert gz(["#a", "#b"]) == gz(["#b", "#a"])
    assert gzip.decompress(gz(["#a"])) == b"#a\n"


def test_a_missing_catalogue_is_only_accepted_on_a_first_run(monkeypatch):
    import urllib.error
    def missing(url, timeout=30):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(build_index, "get", missing)
    with pytest.raises(SystemExit, match="first-run"):
        build_index.previous("https://example.org/names/")
    assert build_index.previous("https://example.org/names/", first_run=True) == {}


def test_previous_refuses_a_published_part_that_does_not_match(monkeypatch):
    files = catalogue({"real": ["#a"], "guesses": ["#g"]})
    files["https://example.org/names/real.txt.gz"] += b"x"
    monkeypatch.setattr(build_index, "get", lambda url, timeout=30: files[url])
    with pytest.raises(SystemExit, match="SHA-256"):
        build_index.previous("https://example.org/names/")
