"""Regression tests for the 0.11.0 catalogue review (openhop-plugin-catalogue PR #58): the hourly cap, Turn off during
API errors, private-channel key changes and Remove without a companion. No radio, no openHop, no network."""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import time
import urllib.error

from Crypto.Cipher import AES

from porchlight import main as pm
from porchlight import porch as pc
from porchlight import speaker as sp
from porchlight.core import Store, channel_hash

KEY_A, KEY_B = "11" * 16, "22" * 16


def grp_txt(secret: bytes, text: str, ts: int | None = None) -> bytes:
    pt = struct.pack("<IB", int(ts or time.time()), 0) + text.encode()
    pt += b"\0" * (-len(pt) % 16)
    ct = AES.new(secret, AES.MODE_ECB).encrypt(pt)
    mac = hmac.new(secret + bytes(16), ct, hashlib.sha256).digest()[:2]
    return bytes([channel_hash(secret)]) + mac + ct


def rec(pid: int, secret_hex: str, text: str) -> dict:
    return {"id": pid, "timestamp": time.time(), "payload": grp_txt(bytes.fromhex(secret_hex), text).hex(),
            "packet_hash": f"PH{pid}"}


class FakeApi:
    """openHop's REST API: sends always succeed; identities and deletes as the test sets them."""
    token = "t"

    def __init__(self, companions=(), delete_error=None, gone_after_delete=True):
        self.companions = [{"name": n} for n in companions]
        self.delete_error, self.gone_after_delete = delete_error, gone_after_delete
        self.calls = []

    def call(self, method, path, body=None, **q):
        self.calls.append((method, path))
        if path == "/api/identities":
            return {"configured_companions": list(self.companions)}
        if path == "/api/delete_identity":
            if self.gone_after_delete:
                self.companions = []
            if self.delete_error:
                raise self.delete_error
            return {}
        if path == "/api/companion/send_channel_message":
            return {"sent": True}
        return {}


def lit_porch(tmp_path, cap=2):
    p = pc.Porch(tmp_path)
    p.configure({"max_posts_per_hour": cap}, FakeApi())
    p.s.update(lit=True, port=5060)
    p.check = lambda: True  # the slot guard has its own test
    p.quiet_air = lambda *a: None
    return p


# ---- hourly limit: every transmission, across restarts ----

def test_the_hourly_cap_survives_a_restart(tmp_path):
    p = lit_porch(tmp_path)
    assert p.say("one") and p.say("two")
    assert not p.say("three")  # at the cap
    p2 = lit_porch(tmp_path)  # a restart: lit state and the hour's posts both come back
    assert p2.lit and len(p2.sent) == 2
    assert not p2.say("three")


def test_posts_older_than_an_hour_do_not_count_after_a_restart(tmp_path):
    p = lit_porch(tmp_path)
    p.say("one")
    p.sent = [(time.time() - 3700, "one")]
    p._save()
    assert lit_porch(tmp_path).say("two")


def test_there_is_no_resend_path_around_the_cap():
    # Porchlight never resends: say() is the only way to transmit, so the cap covers everything it sends.
    assert not hasattr(pc.Porch, "say_again") and not hasattr(pc.Porch, "observers_beyond")
    assert not hasattr(sp.Speaker, "_retry")


def test_an_answer_goes_out_once(tmp_path):
    said = []

    class P:
        lit, area = True, ""

        def say(self, text, **k):
            said.append(text)
            return True

    s = sp.Speaker(tmp_path, P(), Store(), lambda: {})
    assert s._post("@[Ann] hello", "Ann")
    time.sleep(0.1)
    assert said == ["@[Ann] hello"]


# ---- Turn off works while polling fails ----

def test_turn_off_is_handled_while_openhop_refuses_the_token(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"api_token": "t", "light_request": {"id": "7", "action": "off"}}))
    w = pm.Watcher(tmp_path)
    w.porch.s.update(lit=True, port=5060)
    w.updater.tick = lambda req: None

    def refused():
        w.running = False  # one pass of the loop
        raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)

    w.poll = refused
    w.run()
    assert not w.porch.lit and w.porch.s["done_request"] == "7"
    assert w.status.startswith("openHop API 401")


def test_a_config_caught_mid_write_keeps_the_settings(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"private": [{"name": "Fam", "secret": KEY_A}]}))
    w = pm.Watcher(tmp_path)
    w.reload_config()
    cfg.write_text('{"private": [')  # openHop part way through saving
    w.cfg_mtime = None
    assert w.reload_config() is False
    assert "private:Fam" in w.store.channels  # not wiped by a half-written file


# ---- private channel keys follow the settings ----

def test_a_changed_private_key_replaces_the_old_one():
    st = Store()
    st.configure({"private": [{"name": "Fam", "secret": KEY_A}]})
    assert st.add(rec(1, KEY_A, "Ann: on key A"))[0] == "private:Fam"
    st.configure({"private": [{"name": "Fam", "secret": KEY_B}]})
    assert st.add(rec(2, KEY_A, "Ann: still on A")) is None  # A no longer decodes
    assert st.add(rec(3, KEY_B, "Ann: on key B"))[0] == "private:Fam"
    assert st.channels["private:Fam"].secret == bytes.fromhex(KEY_B)


def test_a_removed_private_channel_takes_its_key_with_it():
    st = Store()
    st.configure({"private": [{"name": "Fam", "secret": KEY_A}]})
    st.add(rec(1, KEY_A, "Ann: hi"))
    st.configure({"private": []})
    assert "private:Fam" not in st.channels
    assert st.add(rec(2, KEY_A, "Ann: hi again")) is None
    assert KEY_A not in json.dumps(st.to_state())


def test_a_removed_private_key_does_not_come_back_from_saved_state():
    st = Store()
    st.configure({"private": [{"name": "Fam", "secret": KEY_A}]})
    st.add(rec(1, KEY_A, "Ann: hi"))
    saved = json.loads(json.dumps(st.to_state()))
    st2 = Store()
    st2.load_state(saved)  # the order Watcher.run uses: state first, then the settings
    st2.configure({})
    assert "private:Fam" not in st2.channels and KEY_A not in json.dumps(st2.to_state())
    assert st2.add(rec(2, KEY_A, "Ann: hi again")) is None


# ---- Remove without a companion ----

def test_remove_counts_a_missing_companion_as_removed(tmp_path):
    # Connect pressed, porch never lit: there is no companion, and Remove still finishes.
    p = pc.Porch(tmp_path)
    p.configure({}, FakeApi(companions=()))
    p.remove()
    assert p.s["steps"][0]["ok"] and "nothing to remove" in p.s["steps"][0]["text"]
    assert ("DELETE", "/api/delete_identity") not in p.api.calls


def test_remove_retried_after_the_companion_went_is_ok(tmp_path):
    p = pc.Porch(tmp_path)
    p.configure({}, FakeApi(companions=[pc.IDENTITY], delete_error=RuntimeError("timed out"), gone_after_delete=True))
    p.remove()
    assert p.s["steps"][0]["ok"]  # the delete errored but the companion is gone


def test_remove_reports_a_companion_that_is_still_there(tmp_path):
    p = pc.Porch(tmp_path)
    p.configure({}, FakeApi(companions=[pc.IDENTITY], delete_error=RuntimeError("busy"), gone_after_delete=False))
    p.remove()
    assert not p.s["steps"][0]["ok"] and "busy" in p.s["steps"][0]["text"]
