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


@pytest.fixture
def speaker(tmp_path):
    s = sp.Speaker(tmp_path, FakePorch(), Store(), lambda: VIEW)
    s.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "part_gap_s": 0, "confirm_wait_s": 0})
    return s


def hear_all(s, sender, text, settle=0.3):
    """Every message of an answer (multi-part answers keep posting after the first)."""
    n = len(s.porch.said)
    hear(s, sender, text)
    last, t = len(s.porch.said), time.time()
    while time.time() - t < settle:
        time.sleep(0.05)
        if len(s.porch.said) != last:
            last, t = len(s.porch.said), time.time()
    return s.porch.said[n:]


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
    out = hear_all(speaker, "Newcomer", "help")
    text = " ".join(out)
    assert "Porchlight shows busy channels" in text and "Send porchlight" in text and "Post #name to add one" in text
    assert all(fits(x) for x in out)
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


def test_help_is_complete_for_long_names_and_areas(speaker):
    speaker.porch.area = "Rancho Santa Margarita, CA"
    for name in ("A", "A_Very_Long_Name_23", LONGEST_NAME):
        parts = speaker.pack(speaker.help_segments(), speaker.porch.budget(f"@[{name}] "), speaker.porch.budget(), sep=" ")
        sent = [f"@[{name}] {parts[0]}"] + parts[1:]
        assert all(fits(x) and packet_bytes(x) <= 99 for x in sent), sent
        whole = " ".join(sp.PART_RE_OUT.sub("", x) for x in parts)
        assert "Rancho Santa Margarita, CA" in whole and "Send porchlight" in whole and "Post #name to add one" in whole


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
    parts = sp.Speaker.pack(None, ["a" * 50, "b" * 50], 60)
    assert parts == ["1/2 " + "a" * 50, "2/2 " + "b" * 50]
    assert sp.OTHER_REPLY_RE.match("@[X] 1/2 Busiest, 3 days: #test 4")


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


class _ModeSlot:
    def __init__(self):
        self.modes = []

    def set_path_hash_mode(self, mode):
        self.modes.append(mode)


class _Stats:
    token = "t"

    def __init__(self, stats):
        self.stats = stats

    def call(self, method, path, **kw):
        if isinstance(self.stats, Exception):
            raise self.stats
        return self.stats


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_companion_copies_the_repeaters_path_hash_mode(tmp_path, mode):
    p, fc = pc.Porch(tmp_path), _ModeSlot()
    p.api = _Stats({"config": {"mesh": {"path_hash_mode": mode}}})
    p._match_path_mode(fc)
    assert fc.modes == [mode]


@pytest.mark.parametrize("stats", [{}, {"config": {"mesh": {"path_hash_mode": 9}}}, RuntimeError("no stats")])
def test_path_hash_mode_is_left_alone_when_the_repeater_gives_no_usable_answer(tmp_path, stats):
    p, fc = pc.Porch(tmp_path), _ModeSlot()
    p.api = _Stats(stats)
    p._match_path_mode(fc)  # never stops the porch lighting
    assert fc.modes == []


@pytest.mark.parametrize("text", ["knock", "knock knock", "Knock, knock!", "KNOCK KNOCK?", "knock."])
def test_knock_knock_is_a_knock(speaker, text):
    assert hear(speaker, f"Visitor-{text}", text)


@pytest.mark.parametrize("text", ["knock it off", "knock knock knock", "who knocks", "knockout"])
def test_other_knock_words_are_not(speaker, text):
    assert not hear(speaker, f"Visitor-{text}", text)


# ---- nothing is cut, @[who] only on the first message ----

MANY = {"channels": [chan(f"#channel-with-a-long-name-{i}", 50 - i) for i in range(8)]
        + [chan(f"#brand-new-{i}", 1, kind="discovered", found_at=NOW - 60 * i) for i in range(4)],
        "regions": {"window_days": 3, "top": [], "unknown": 0}}


def test_a_long_answer_sends_every_channel_and_tags_only_the_first(speaker):
    speaker.view_fn = lambda: MANY
    speaker.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "part_gap_s": 0, "top_count": 8})
    speaker.mark_greeted("Ann")
    out = hear_all(speaker, "Ann", "porchlight")
    assert len(out) >= 3
    assert out[0].startswith("@[Ann] 1/") and not any("@[" in x for x in out[1:])
    assert all(sp.PART_RE_OUT.match(x) for x in out[1:])
    text = " ".join(out)
    for i in range(8):
        assert f"#channel-with-a-long-name-{i}" in text, i  # none dropped
    for i in range(4):
        assert f"#brand-new-{i}" in text
    assert all(fits(x) and not x.endswith("...") for x in out)


def test_three_names_sent_in_all_get_an_answer(speaker):
    speaker.porch.area = "Rancho Santa Margarita, CA"
    out = hear_all(speaker, LONGEST_NAME, "#first-long-name #second-long-name #third-long-name")
    text = " ".join(out)
    assert all(n in text for n in ("#first-long-name", "#second-long-name", "#third-long-name"))
    assert all(fits(x) and not x.endswith("...") for x in out)
    assert out[0].startswith(f"@[{LONGEST_NAME}]") and not any("@[" in x for x in out[1:])


def test_a_part_asked_for_again_fits_with_or_without_the_tag(speaker):
    speaker.view_fn = lambda: MANY
    speaker.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "part_gap_s": 0, "top_count": 8})
    speaker.mark_greeted(LONGEST_NAME)
    first = hear_all(speaker, LONGEST_NAME, "porchlight")
    n = len(first)
    again = hear_all(speaker, LONGEST_NAME, f"porchlight 2 of {n}")
    assert len(again) == 1 and fits(again[0]) and "2/" in again[0]


# ---- porches never answer each other ----

def test_nothing_a_porch_says_is_answered_even_under_another_name(speaker):
    speaker.view_fn = lambda: MANY
    speaker.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "part_gap_s": 0, "top_count": 8})
    (speaker.path.parent / "other").mkdir(exist_ok=True)
    other = sp.Speaker(speaker.path.parent / "other", FakePorch(), Store(), lambda: MANY)
    other.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "part_gap_s": 0, "top_count": 8})
    porch_output = hear_all(other, "Ann", "porchlight") + hear_all(other, "Bo", "help") \
        + hear_all(other, "Cy", "#first-long-name #second-long-name #third-long-name") + ["#wx (Springfield, IL)"]
    assert len(porch_output) > 4
    for line in porch_output:  # heard from a porch whose name isn't "Porchlight" (old or renamed install)
        assert hear(speaker, "Renamed Porch", line) == [], line


def test_two_porches_hearing_each_other_answer_a_knock_once(tmp_path):
    """Wire two porches together: everything one says, the other hears. One knock, one answer, then silence."""
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(), b_dir.mkdir()
    a = sp.Speaker(a_dir, FakePorch(), Store(), lambda: VIEW)
    b = sp.Speaker(b_dir, FakePorch(), Store(), lambda: VIEW)
    a.configure({"reply_hop_seconds": 0.0, "reply_jitter_seconds": 0, "part_gap_s": 0})
    b.configure({"reply_hop_seconds": 0.4, "reply_jitter_seconds": 0, "part_gap_s": 0})  # b is farther away

    def wire(src, dst, name):
        say = src.porch.say

        def relay(text, **k):
            say(text, **k)
            dst.hear({"t": time.time(), "sender": name, "text": text, "hops": 1})
            return True
        src.porch.say = relay
    wire(a, b, pc.NODE_NAME)
    wire(b, a, "Old Porch")  # one of them even under another name
    for s in (a, b):
        s.hear({"t": time.time(), "sender": "Ann", "text": "porchlight", "hops": 1})
    time.sleep(1.5)
    assert any(x.startswith("@[Ann]") for x in a.porch.said)
    assert not b.porch.said  # heard a's answer, stayed quiet
    assert all(not x.startswith("@[Porchlight]") and not x.startswith("@[Old Porch]") for x in a.porch.said)


def test_every_porch_that_heard_a_name_answers_and_the_rest_stay_quiet(tmp_path):
    """Three porches hear '#x'. Near (no traffic) confirms first; far-with-traffic answers too; far-without stays quiet."""
    from porchlight.core import Channel
    porches = {}
    for name, delay, heard in (("near", 0.0, False), ("far_heard", 0.3, True), ("far_quiet", 0.6, False)):
        d = tmp_path / name
        d.mkdir()
        st = Store()
        if heard:
            st.channels["#x"] = Channel("#x", "#x", "discovered", hashtag_secret("#x"), 100)
            st.channels["#x"].found_at, st.channels["#x"].total = time.time() - 86400, 7
        s = sp.Speaker(d, FakePorch(), st, lambda: VIEW)
        s.configure({"reply_hop_seconds": delay, "reply_jitter_seconds": 0, "part_gap_s": 0, "confirm_wait_s": 0.8})
        porches[name] = s

    def wire(src_name):
        src = porches[src_name]
        say = src.porch.say

        def relay(text, **k):
            say(text, **k)
            for other_name, other in porches.items():
                if other_name != src_name:
                    other.hear({"t": time.time(), "sender": pc.NODE_NAME, "text": text, "hops": 1})
            return True
        src.porch.say = relay
    for n in porches:
        wire(n)
    for s in porches.values():
        s.hear({"t": time.time(), "sender": "Ann", "text": "#x", "hops": 1})
    time.sleep(2.5)
    assert porches["far_heard"].porch.said and "is already on" in porches["far_heard"].porch.said[0]
    assert porches["near"].porch.said == [] and porches["far_quiet"].porch.said == []  # the porch that heard it spoke


def test_when_no_porch_heard_a_name_the_nearest_confirms(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    near, far = sp.Speaker(a, FakePorch(), Store(), lambda: VIEW), sp.Speaker(b, FakePorch(), Store(), lambda: VIEW)
    near.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "confirm_wait_s": 0.2})
    far.configure({"reply_hop_seconds": 0.5, "reply_jitter_seconds": 0, "confirm_wait_s": 0.2})
    say = near.porch.say

    def relay(text, **k):
        say(text, **k)
        far.hear({"t": time.time(), "sender": pc.NODE_NAME, "text": text, "hops": 1})
        return True
    near.porch.say = relay
    for s in (near, far):
        s.hear({"t": time.time(), "sender": "Ann", "text": "#brand-new-name", "hops": 1})
    time.sleep(1.5)
    assert near.porch.said == ["@[Ann] #brand-new-name added to Porchlight Springfield, IL"]
    assert far.porch.said == []


def test_a_configured_channel_never_heard_is_not_heard_here(speaker):
    from porchlight.core import Channel
    speaker.store.channels["#cfg"] = Channel("#cfg", "#cfg", "hashtag", hashtag_secret("#cfg"), 100)  # total 0
    speaker.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "confirm_wait_s": 0.5})
    speaker.replied_add["ann"] = time.time()  # another porch already answered Ann
    assert hear_all(speaker, "Ann", "#cfg", settle=0.8) == []


def test_a_reply_to_porchlight_is_still_a_knock(speaker):
    assert hear(speaker, "Alice", "@[Porchlight] porchlight")
    out = hear(speaker, "Bob", "@[Porchlight] #zzqtest")
    assert out and "#zzqtest" in out[0]
    assert not hear(speaker, "Cat", "@[Dan] porchlight")  # talking to someone else


def test_the_whole_answer_or_nothing_at_the_hourly_cap(speaker):
    speaker.view_fn = lambda: MANY
    speaker.configure({"reply_hop_seconds": 0, "reply_jitter_seconds": 0, "part_gap_s": 0, "top_count": 8})
    speaker.porch.posts_left = lambda: 1  # room for one post, the answer needs several
    speaker.mark_greeted("Ann")
    assert hear_all(speaker, "Ann", "porchlight") == []


def test_packing_never_cuts_a_name_drops_one_or_overflows():
    import random
    rnd = random.Random(7)
    word = lambda n: "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz-") for _ in range(n)).strip("-") or "x"
    for _ in range(3000):
        sender = "S" * rnd.randint(1, 31)
        first = pc.PACKET_TEXT - len(pc.NODE_NAME) - 2 - len(f"@[{sender}] ".encode())
        rest = pc.PACKET_TEXT - len(pc.NODE_NAME) - 2
        names = ["#" + word(rnd.randint(1, 31)) for _ in range(rnd.randint(1, 12))]
        segs = [f"Busiest, 3 days: " + ", ".join(f"{n} {rnd.randint(1, 99)}" for n in names[:6]),
                "New in 7 days: " + ", ".join(names[6:]) if names[6:] else ""]
        parts = sp.Speaker.pack(None, segs, first, rest, hint="Know a channel? Post #name here")
        assert len(parts[0].encode()) <= first and all(len(x.encode()) <= rest for x in parts[1:]), parts
        tokens = set(" ".join(sp.PART_RE_OUT.sub("", x) for x in parts).replace(",", " ").replace("|", " ").split())
        for n in names:
            assert n in tokens, (n, parts)  # whole, never cut
