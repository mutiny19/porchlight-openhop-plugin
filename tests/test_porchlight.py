"""Porchlight tests: no radio, no openHop, no network. Run with `python -m pytest -q`."""

import hashlib
import json
import hmac
import struct
import time

import pytest
from Crypto.Cipher import AES

from porchlight import porch as pc
from porchlight import speaker as sp
from porchlight.core import Store, channel_hash, hashtag_secret

NOW = time.time()


def chan(name, n, kind="hashtag", sender="someone", found_at=None):
    msgs = [{"t": NOW - 60 * i, "sender": f"{sender}{i % 3}", "text": "hi", "hops": 1} for i in range(n)]
    return {"key": name, "name": name, "kind": kind, "msgs": msgs, "found_at": found_at, "last": NOW}


VIEW = {"channels": [chan("#test", 40), chan("#wx", 12), chan("#echo", 5), chan("#wardriving", 90),
                     chan("#nky", 1, kind="discovered", found_at=NOW - 3600)],
        "regions": {"window_days": 3, "top": [], "unknown": 0}}


class FakePorch:
    lit, area = True, "Springfield, IL"

    def __init__(self):
        self.said = []

    def budget(self, tag=""):
        return pc.PACKET_TEXT - len(pc.NODE_NAME) - 2 - len(tag.encode())

    def say(self, text, quiet_s=5, max_s=20):
        self.said.append(text)
        return True

    def is_mine(self, sender, text):
        return sender == pc.NODE_NAME and text in self.said

    def observers_beyond(self, ph):
        return None  # no CoreScope configured


@pytest.fixture
def speaker(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.threading, "Timer", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    s = sp.Speaker(tmp_path, FakePorch(), Store(), lambda: VIEW)
    s.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0})
    return s


def hear(s, sender, text, hops=1, age=0):
    n = len(s.porch.said)
    s.hear({"t": time.time() - age, "sender": sender, "text": text, "hops": hops})
    for _ in range(50):
        if len(s.porch.said) > n:
            break
        time.sleep(0.02)
    time.sleep(0.05)
    return s.porch.said[n:]


def fits(text):
    return len(f"{pc.NODE_NAME}: {text}".encode()) <= pc.PACKET_TEXT


def packet_bytes(text):
    """GRP_TXT payload size on air: channel hash + MAC + AES blocks of (timestamp, flags, "<node>: <text>")."""
    plain = 5 + len(f"{pc.NODE_NAME}: {text}".encode())
    return 3 + -(-plain // 16) * 16


LONGEST_NAME = "N" * 31  # MeshCore node names are at most 31 bytes


def test_first_knock_gets_one_welcome_then_the_list(speaker):
    out = hear(speaker, "Visitor", "porchlight")
    assert 1 <= len(out) <= 2 and "Welcome! Active near Springfield, IL: #test" in out[0]  # + new channels
    assert all(fits(x) for x in out)
    speaker.knocked.clear()
    out = hear(speaker, "Visitor", "#porchlight")
    assert out and "Busiest, 3 days: #test 40" in out[0] and all(fits(x) for x in out)


def test_help_does_not_use_up_the_welcome(speaker):
    out = hear(speaker, "Newcomer", "help")
    assert len(out) == 1 and "Porchlight shows busy channels" in out[0] and "Send porchlight" in out[0] and fits(out[0])
    assert speaker.is_new_visitor("Newcomer")


@pytest.mark.parametrize("text", ["hello all", "help me find channels", "@[Bob] porchlight"])
def test_no_knock_no_answer(speaker, text):
    assert hear(speaker, "Someone", text) == []


def test_other_porches_and_old_traffic_are_never_answered(speaker):
    assert hear(speaker, pc.NODE_NAME, "porchlight") == []
    assert hear(speaker, "Late", "porchlight", age=600) == []


def test_stays_quiet_when_another_porch_answers_first(speaker):
    speaker.configure({"reply_hop_seconds": 0.3, "reply_jitter_seconds": 0})
    speaker.hear({"t": time.time(), "sender": "Cat", "text": "porchlight", "hops": 1})
    speaker.hear({"t": time.time(), "sender": pc.NODE_NAME, "text": "@[Cat] Welcome! Channels active near Shelbyville, IL today: #test", "hops": 2})
    time.sleep(0.6)
    assert not any("@[Cat]" in x for x in speaker.porch.said)
    assert not speaker.is_new_visitor("Cat")  # any porch's welcome counts


def test_names_sent_in(speaker):
    out = hear(speaker, "Dan", "#newchan")
    assert out == ["@[Dan] #newchan added to Porchlight Springfield, IL"]
    assert "#newchan" in speaker.store.submitted


def test_help_fits_one_packet_for_long_names_and_areas(speaker):
    speaker.porch.area = "Rancho Santa Margarita, CA"
    for tag in ("@[A] ", "@[A_Very_Long_Name_23] ", f"@[{LONGEST_NAME}] "):
        text = speaker.help_text(speaker.porch.budget(tag))
        assert text.startswith("Porchlight shows") and "Send porchlight" in text
        assert fits(tag + text) and packet_bytes(tag + text) <= 99


def test_every_reply_stays_one_small_packet_even_for_the_longest_name(speaker):
    """Long sender names shrink the room for text; replies must split into more parts, never grow past 99 B."""
    for knock in ("porchlight", "#porchlight"):
        out = hear(speaker, LONGEST_NAME, knock)
        assert out, knock
        for x in out:
            assert packet_bytes(x) <= 99, (knock, x)
            assert not x.endswith("..."), x  # channels are moved to another part, not cut off
        speaker.knocked.clear()


def test_part_labels_are_short_and_still_recognised():
    parts = sp.Speaker.pack(type("S", (), {"c": lambda self, k, d: d})(), ["a" * 50, "b" * 50], 60)
    assert parts == ["1/2 " + "a" * 50, "2/2 " + "b" * 50]
    assert sp.OTHER_REPLY_RE.match("@[X] 1/2 Busiest, 3 days: #test 4")


def test_a_resend_reuses_the_original_timestamp(speaker):
    """Same text + same timestamp = the identical packet, so nobody sees a resend twice."""
    sent = []
    speaker.porch.say_again = lambda text, ts, **k: sent.append((text, ts)) or True
    speaker.c = lambda k, d: [] if k == "local_repeaters" else d
    msg = {"sender": pc.NODE_NAME, "text": "@[Ann] 1/1 Busiest, 3 days: #test 40", "t": time.time(), "sts": 1790000000,
           "heard": 1}
    speaker.view_fn = lambda: {"channels": [{"key": sp.CHANNEL, "msgs": [msg]}]}
    speaker._retry(msg["text"], "Ann", msg["t"])
    assert sent == [(msg["text"], 1790000000)]


def _grp_txt(name, text):
    secret = hashtag_secret(name)
    pt = struct.pack("<IB", int(NOW), 0) + text.encode()
    pt += b"\0" * (-len(pt) % 16)
    ct = AES.new(secret, AES.MODE_ECB).encrypt(pt)
    mac = hmac.new(secret + bytes(16), ct, hashlib.sha256).digest()[:2]
    return bytes([channel_hash(secret)]) + mac + ct


def test_store_always_reads_porchlight_and_returns_new_messages():
    st = Store()
    st.configure({})
    rec = {"id": 1, "timestamp": NOW, "payload": _grp_txt("#porchlight", "Visitor: porchlight").hex(), "packet_hash": "AA"}
    key, m = st.add(rec)
    assert key == "#porchlight" and m["sender"] == "Visitor" and m["text"] == "porchlight"
    assert st.add(dict(rec, id=2)) is None  # a second copy is counted, not a new message


def test_guard_goes_dark_when_the_slot_changes(tmp_path, monkeypatch):
    p = pc.Porch(tmp_path)
    p.s.update(lit=True, port=5060)

    class Slot:
        def __init__(self, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get_channel(self, idx):
            return "#test", hashtag_secret("#test")

    monkeypatch.setattr(pc, "FrameClient", Slot)
    assert p.check() is False and p.lit is False and "channel was changed" in p.s["reason"]


def test_help_then_knock_both_get_answers(speaker):
    assert hear(speaker, "Asker", "help")
    assert hear(speaker, "Asker", "porchlight")


@pytest.mark.parametrize("heard, expect", [(5, "none"), (0, "fresh"), (None, "identical")])
def test_resend_asks_corescope_first(speaker, heard, expect):
    """Heard beyond us: no resend. Nobody heard it: a fresh post. CoreScope can't tell: the identical packet."""
    calls = []
    speaker.porch.observers_beyond = lambda ph: heard
    speaker.porch.say_again = lambda text, ts, **k: calls.append("identical") or True
    speaker.porch.say = lambda text, **k: calls.append("fresh") or True
    speaker.c = lambda k, d: [] if k == "local_repeaters" else d
    msg = {"sender": pc.NODE_NAME, "text": "@[Ann] 1/1 Busiest, 3 days: #test 40", "t": time.time(), "sts": 1790000000,
           "heard": 1, "ph": "AB12"}
    speaker.view_fn = lambda: {"channels": [{"key": sp.CHANNEL, "msgs": [msg]}]}
    speaker._retry(msg["text"], "Ann", msg["t"])
    assert calls == ([] if expect == "none" else [expect])


def test_observers_beyond_needs_our_own_upload(tmp_path, monkeypatch):
    p = pc.Porch(tmp_path)
    p.cfg = {"reach_check_url": "https://cs.example", "reach_observer": "MyRepeater"}

    class Resp:
        def __init__(self, names):
            self.body = json.dumps({"observations": [{"observer_name": n} for n in names]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, *a):
            return self.body

    for names, want in ((["MyRepeater", "A", "B"], 2), (["MyRepeater"], 0), (["A", "B"], None), ([], None)):
        monkeypatch.setattr(pc.urllib.request, "urlopen", lambda req, timeout=10, n=names: Resp(n))
        assert p.observers_beyond("AB12") == want, names
    p.cfg = {}
    assert p.observers_beyond("AB12") is None  # off unless configured


def test_lights_without_a_location_when_the_owner_typed_an_area(tmp_path, monkeypatch):
    # Many repeaters never set a location; a typed area is enough.
    p = pc.Porch(tmp_path)

    class NoLocation:
        token = "t"

        def call(self, method, path, **kw):
            return {"config": {"repeater": {}}}

    class Slot:
        def __init__(self, port):
            self.slot = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def set_channel(self, idx, name, secret):
            self.slot = (name, secret)

        def get_channel(self, idx):
            return self.slot

    monkeypatch.setattr(pc, "FrameClient", Slot)
    monkeypatch.setattr(p, "_companion", lambda: 5060)
    monkeypatch.setattr(pc, "area_name", lambda lat, lon: pytest.fail("no lookup without a location"))
    p.configure({}, NoLocation())
    p.light()
    assert p.lit is False and "no location" in p.s["reason"]
    p.configure({"area": "Springfield, IL"}, NoLocation())
    p.light()
    assert p.lit is True and p.area == "Springfield, IL"


def test_a_fresh_install_announces_nothing_from_its_first_scan(speaker, monkeypatch):
    # The first scan names many channels at once (backfill + name lists): that's not news, so no burst on air.
    from types import SimpleNamespace
    now = time.time()
    speaker.porch.lit = True
    speaker.store.channels = {"#newtown": SimpleNamespace(key="#newtown", kind="discovered", found_at=now - 60,
                                                         msgs=[{"t": now - 120, "sender": "someone"}])}
    spawned = []
    monkeypatch.setattr(speaker, "_spawn", lambda fn, *a: spawned.append(a[0]))
    speaker.consider_news()
    assert spawned == []
    # Past the warm-up, a channel named just now with traffic is news again.
    speaker.st["since"] = now - sp.WARMUP - 600
    speaker.consider_news()
    assert spawned == ["#newtown"]


def test_light_skips_a_port_a_deleted_companion_still_holds(tmp_path, monkeypatch):
    # openHop's delete leaves the old companion's frame server on its port until a restart; a new companion
    # created there can't start and isn't known by name, so sends fail. Porchlight must pick another port.
    p = pc.Porch(tmp_path)
    created, state = [], {"running": False}

    class Api:
        token = "t"

        def call(self, method, path, body=None, **q):
            if path == "/api/identities":
                return {"configured_companions": [{"name": "pong", "settings": {"tcp_port": 5061}}]}
            if path == "/api/companion/self_info":
                if not state["running"]:
                    raise pc.PorchError("Companion 'porchlight' not found")
                return {}
            if path == "/api/create_identity":
                created.append(body["settings"]["tcp_port"])
                state["running"] = True
            return {}

    p.configure({}, Api())
    monkeypatch.setattr(pc, "port_in_use", lambda port: port == 5060)  # the deleted companion's frame server
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    assert p._companion() == 5062 and created == [5062]


def test_light_fails_clearly_when_openhop_cannot_start_the_companion(tmp_path, monkeypatch):
    p = pc.Porch(tmp_path)

    class Api:
        token = "t"

        def call(self, method, path, body=None, **q):
            if path == "/api/identities":
                return {"configured_companions": []}
            if path == "/api/companion/self_info":
                raise pc.PorchError("not found")
            return {}

    p.configure({}, Api())
    monkeypatch.setattr(pc, "port_in_use", lambda port: False)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    with pytest.raises(pc.PorchError, match="Restart openHop"):
        p._companion()


def test_light_retries_with_a_fresh_key_when_openhop_refuses_one(tmp_path, monkeypatch):
    # A deleted companion's one-byte key prefix stays reserved until openHop restarts; a new random key that
    # repeats it is saved but never started. The next attempt (new key, new port) works.
    p = pc.Porch(tmp_path)
    calls = {"create": 0, "delete": 0}

    class Api:
        token = "t"

        def call(self, method, path, body=None, **q):
            if path == "/api/identities":
                return {"configured_companions": []}
            if path == "/api/companion/self_info":
                if calls["create"] < 2:
                    raise pc.PorchError("not found")
                return {}
            if path == "/api/create_identity":
                calls["create"] += 1
            if path == "/api/delete_identity":
                calls["delete"] += 1
            return {}

    p.configure({}, Api())
    monkeypatch.setattr(pc, "port_in_use", lambda port: False)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    assert p._companion() == 5060 and calls == {"create": 2, "delete": 1}


def _grp(secret, text, ts=None):
    pt = struct.pack("<IB", int(ts or time.time()), 0) + text.encode()
    pt += bytes(-len(pt) % 16)
    ct = AES.new(secret, AES.MODE_ECB).encrypt(pt)
    return bytes([channel_hash(secret)]) + hmac.new(secret + bytes(16), ct, hashlib.sha256).digest()[:2] + ct


def test_a_guessed_name_must_decode_to_a_real_channel_message():
    # With ~170k guesses, ~700 names share each channel hash: a wrong one passing the 2-byte MAC must still fail.
    from porchlight.core import decrypt
    s = hashtag_secret("#fishers-wx")
    assert decrypt(s, _grp(s, "N0CALL: storm at 5"), strict=True)
    assert not decrypt(s, _grp(s, "no sender here"), strict=True)
    assert not decrypt(s, _grp(s, "N0CALL: old clock", ts=1500000000), strict=True)
    assert decrypt(s, _grp(s, "N0CALL: old clock", ts=1500000000))  # a known channel still shows it


def test_list_source_reads_a_gzipped_text_file(tmp_path):
    import gzip
    from porchlight import sources
    p = tmp_path / "names.txt.gz"
    with gzip.open(p, "wt") as f:
        f.write("// comment\n#Fishers-WX\nindymesh\n\nnot a name!\n")
    assert sources.fetch({"type": "list", "url": p.as_uri()}) == {"#fishers-wx", "#indymesh"}


def test_new_guesses_only_try_names_with_the_packets_channel_hash(monkeypatch):
    from porchlight import core
    from porchlight.core import Channel
    st = Store()
    s = hashtag_secret("#fishers-wx")
    unk = st.channels["x"] = Channel("x", "x", "unknown", None, 100)
    unk.msgs.append({"t": NOW, "raw": _grp(s, "N0CALL: storm").hex(), "ph": "p1"})
    unk.total = 1
    tried = []
    real = core.decrypt
    monkeypatch.setattr(core, "decrypt", lambda sec, pl, strict=False: tried.append(sec) or real(sec, pl, strict))
    names = {"#fishers-wx"} | {f"#n{i}" for i in range(5000)}
    st._set_guesses(names)
    assert "#fishers-wx" in st.channels and st.channels["#fishers-wx"].kind == "discovered"
    assert len(tried) < 100  # ~5000/256 share the hash, not all 5000


def test_names_dropped_from_a_source_stop_being_tried():
    st = Store()
    st.configure({"guesses": ["#mine"]})
    st.set_source_names({"#old1", "#old2", "#keep"})
    st.set_source_names({"#keep", "#new"})
    assert {"#keep", "#new", "#mine"} <= st.guesses and not {"#old1", "#old2"} & st.guesses
