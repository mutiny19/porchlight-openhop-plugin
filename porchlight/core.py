"""Channel decryption and the per-channel message store."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import struct
import time
from collections import deque

from Crypto.Cipher import AES

from .wordlist import GUESSES

PUBLIC_SECRET = "8b3387e9c5cdea6ac9e5edbaa115cd72"
HASHTAG_RE = re.compile(r"(?<![\w#])#(?=[a-z0-9_-]*[a-z])[a-z0-9][a-z0-9_-]{0,30}", re.I)  # needs a letter: "#2" is not a channel
HOUR = 3600
MAX_SUBMITTED = 5000


def hashtag_secret(name: str) -> bytes:
    """A hashtag channel's key is derived from its (lower-cased) name, so anyone can compute it."""
    return hashlib.sha256(name.lower().encode("utf-8")).digest()[:16]


def channel_hash(secret: bytes) -> int:
    return hashlib.sha256(secret).digest()[0]


GUESS_MIN_TS = 1577836800  # 2020-01-01: a guessed key must decode to a message sent after this
SENDER_RE = re.compile(r"^[^:\x00-\x1f]{1,32}: ")


def decrypt(secret: bytes, payload: bytes, strict: bool = False):
    """GRP_TXT payload = [channel hash][2-byte MAC][AES-128-ECB ciphertext]. Returns (ts, text) or None.

    strict (for guessed names): with a big name list, hundreds of names share each one-byte channel hash and the MAC is
    only 2 bytes, so also require what every channel message looks like: "sender: text" and a timestamp after 2020.
    Known channels skip this, so a node with a broken clock still shows up."""
    mac, ct = payload[1:3], payload[3:]
    if not ct or len(ct) % 16:
        return None
    if hmac.new(secret + bytes(16), ct, hashlib.sha256).digest()[:2] != mac:
        return None
    pt = AES.new(secret, AES.MODE_ECB).decrypt(ct)
    ts, _flags = struct.unpack_from("<IB", pt)
    text = pt[5:].split(b"\0", 1)[0].decode("utf-8", "replace")
    if not text or sum(c.isprintable() for c in text) < len(text) * 0.9:
        return None  # a 2-byte MAC collides 1 in 65536; garbage text means a wrong guess
    if strict and (ts < GUESS_MIN_TS or not SENDER_RE.match(text)):
        return None
    return ts, text


def _hops(rec: dict) -> int | None:
    try:
        path = json.loads(rec.get("original_path") or "[]")
        return len(path) if isinstance(path, list) else None
    except (TypeError, ValueError):
        return None


class Channel:
    def __init__(self, key: str, name: str, kind: str, secret: bytes | None, per_channel: int):
        self.key = key  # "#openhouse" / "public" / "hash:d9"
        self.name = name
        self.kind = kind  # public | hashtag | private | discovered | unknown
        self.secret = secret
        self.msgs: deque = deque(maxlen=per_channel)
        self.total = 0
        self.first = None
        self.last = None
        self.found_at = None

    def to_state(self) -> dict:
        return {"key": self.key, "name": self.name, "kind": self.kind,
                "secret": self.secret.hex() if self.secret else None, "msgs": list(self.msgs),
                "total": self.total, "first": self.first, "last": self.last, "found_at": self.found_at}

    @classmethod
    def from_state(cls, d: dict, per_channel: int) -> "Channel":
        c = cls(d["key"], d["name"], d["kind"], bytes.fromhex(d["secret"]) if d.get("secret") else None, per_channel)
        c.msgs.extend(d.get("msgs", []))
        c.total, c.first, c.last, c.found_at = d.get("total", 0), d.get("first"), d.get("last"), d.get("found_at")
        return c


class Store:
    """Everything the dashboard shows, rebuilt from openHop's packet log and kept across restarts."""

    def __init__(self, per_channel: int = 100):
        self.per_channel = per_channel
        self.channels: dict[str, Channel] = {}
        self.by_packet: dict[str, tuple[str, dict]] = {}  # packet_hash -> (channel key, msg), for duplicate copies
        self.seen_ids: dict[int, float] = {}
        self.guesses: set[str] = set()
        self.learned: set[str] = set()  # #tags seen in decrypted chat
        self.source_names: set[str] = set()  # from name_sources (public channel lists)
        self.submitted: set[str] = set()  # names people sent in with `porchlight add` (kept for good)
        self.cfg_guesses: set[str] = set()  # "Extra names to try" from the settings
        self.discovery = True
        self.tried: set[str] = set()
        self._index: dict[int, list[str]] = {}  # channel hash -> guess names
        self.dirty = True

    # ---- configuration ----
    def configure(self, cfg: dict) -> None:
        self.per_channel = int(cfg.get("per_channel", 100))
        known = [("public", "Public", "public", bytes.fromhex(PUBLIC_SECRET))]
        for name in ["#porchlight"] + list(cfg.get("hashtags", [])):  # the porch's own channel is always read
            name = "#" + name.strip().lstrip("#").lower()
            if len(name) > 1:
                known.append((name, name, "hashtag", hashtag_secret(name)))
        for ch in cfg.get("private", []):
            try:
                secret = bytes.fromhex(ch["secret"].strip())
                if len(secret) == 16 and ch.get("name"):
                    known.append((f"private:{ch['name']}", ch["name"], "private", secret))
            except (KeyError, ValueError, AttributeError):
                continue
        for key, name, kind, secret in known:
            c = self.channels.get(key)
            if c is None:
                self.channels[key] = Channel(key, name, kind, secret, self.per_channel)
                self.dirty = True
            elif c.kind == "discovered" and kind == "hashtag":
                c.kind = kind  # the user added a channel we had already discovered
                self.dirty = True
        self.discovery = bool(cfg.get("discovery", True))
        self.cfg_guesses = {"#" + g.strip().lstrip("#").lower() for g in cfg.get("guesses", []) if g.strip().lstrip("#")}
        self._set_guesses(self._all_guesses())

    def _all_guesses(self) -> set[str]:
        auto = set(GUESSES) | self.learned | self.source_names if self.discovery else set()
        return auto | self.cfg_guesses | self.submitted

    def set_source_names(self, names: set[str]) -> None:
        """Names from the public lists (sources.py); new ones are tried against stored unread packets at once."""
        new, gone = names - self.source_names, self.source_names - names
        self.source_names = set(names)
        if gone:  # a list shrank or a source was removed: rebuild, so dropped names stop being tried
            self._set_guesses(self._all_guesses())
        elif new and self.discovery:
            self._set_guesses(self.guesses | new)

    def submit(self, name: str, t: float) -> dict:
        """A name someone sent in (`porchlight add #name`): kept for good and tried against stored unread packets at once.
        result: named (just put a name to traffic heard here), known (already named), saved (not heard here yet), invalid."""
        name = "#" + str(name).strip().lstrip("#").lower()
        if not HASHTAG_RE.fullmatch(name):
            return {"name": name, "result": "invalid"}
        if name not in self.submitted and len(self.submitted) < MAX_SUBMITTED:
            self.submitted.add(name)
            self.dirty = True
        if name not in self.guesses:
            self._set_guesses(self.guesses | {name})
        c = self.channels.get(name)
        if c is None:
            return {"name": name, "result": "saved"}
        new = c.found_at is not None and c.found_at >= t - 120  # a #tag in the same chat message may have named it first
        return {"name": name, "result": "named" if new else "known", "total": c.total, "last": c.last}

    def _set_guesses(self, guesses: set[str]) -> None:
        self.guesses = guesses
        self._index = {}
        for name in guesses:
            self._index.setdefault(channel_hash(hashtag_secret(name)), []).append(name)
        self.tried &= guesses
        self._retry_unknown()

    # ---- packets ----
    def _secrets_for(self, h: int):
        for c in self.channels.values():
            if c.secret is not None and channel_hash(c.secret) == h:
                yield c

    def _guess(self, h: int, payload: bytes):
        for name in sorted(self._index.get(h, [])):
            if name in self.channels:
                continue
            secret = hashtag_secret(name)
            if decrypt(secret, payload, strict=True):
                c = Channel(name, name, "discovered", secret, self.per_channel)
                c.found_at = time.time()
                self.channels[name] = c
                return c
        return None

    def add(self, rec: dict):
        """File one packet. Returns (channel key, msg) for a newly decoded message, else None."""
        pid = rec.get("id")
        if pid in self.seen_ids:
            return None
        self.seen_ids[pid] = rec.get("timestamp") or time.time()
        try:
            payload = bytes.fromhex(rec.get("payload") or "")
        except ValueError:
            return None
        if len(payload) < 4:
            return None
        phash = rec.get("packet_hash") or f"id{pid}"
        snr, rssi = rec.get("snr"), rec.get("rssi")
        if phash in self.by_packet:  # another copy of a message we already have: count it, keep the best signal
            _, m = self.by_packet[phash]
            m["heard"] += 1
            # Which repeaters relayed this copy: the resend check ignores relays by our own
            # next-door repeaters (local_repeaters), which repeat everything and don't show the message got any farther.
            try:
                path = json.loads(rec.get("original_path") or "[]")
            except (TypeError, ValueError):
                path = []
            if isinstance(path, list) and path and len(m.setdefault("vias", [])) < 8:
                m["vias"].append([str(x) for x in path])
            if snr is not None and (m.get("snr") is None or snr > m["snr"]):
                m["snr"], m["rssi"] = snr, rssi
            self.dirty = True
            return None
        t = rec.get("timestamp") or time.time()
        h = payload[0]
        m = {"t": t, "hops": _hops(rec), "path": rec.get("original_path"), "snr": snr, "rssi": rssi,
             "heard": 1, "len": len(payload), "ph": phash}
        chan, res = None, None
        for c in self._secrets_for(h):
            res = decrypt(c.secret, payload)
            if res:
                chan = c
                break
        if chan is None and self.guesses:
            chan = self._guess(h, payload)
            res = decrypt(chan.secret, payload) if chan else None
        if chan is None:
            chan = self.channels.get(f"hash:{h:02x}")
            if chan is None:
                chan = self.channels[f"hash:{h:02x}"] = Channel(f"hash:{h:02x}", f"{h:02X}", "unknown", None, self.per_channel)
            m["raw"] = payload.hex()  # kept so a later guess can still decode it
        else:
            sts, text = res
            sender, sep, body = text.partition(": ")
            m.update(sts=sts, sender=sender if sep else "", text=body if sep else text)
            self._learn_hashtags(text)
        self._file(chan, m)
        self.by_packet[phash] = (chan.key, m)
        return (chan.key, m) if "text" in m else None

    def _file(self, chan: Channel, m: dict) -> None:
        chan.msgs.append(m)
        chan.total += 1
        chan.first = min(chan.first or m["t"], m["t"])
        chan.last = max(chan.last or 0, m["t"])
        self.dirty = True

    def _learn_hashtags(self, text: str) -> None:
        """People name channels in chat ("join #openhouse"); every #tag seen becomes a guess."""
        if not self.discovery:
            return
        new = {t.lower() for t in HASHTAG_RE.findall(text)} - self.guesses
        if new:
            self.learned |= new
            self._set_guesses(self.guesses | new)

    def _retry_unknown(self) -> None:
        """Try new guesses against messages still filed under an unknown channel hash."""
        pending = self.guesses - self.tried
        if not pending:
            return
        self.tried |= pending
        for key in [k for k, c in self.channels.items() if c.kind == "unknown"]:
            unk = self.channels[key]
            for m in list(unk.msgs):
                payload = bytes.fromhex(m["raw"])
                # only names whose channel hash is this packet's (about 1 in 256 of them), not the whole list
                for name in sorted(n for n in self._index.get(payload[0], ()) if n in pending):
                    secret = hashtag_secret(name)
                    if not (res := decrypt(secret, payload, strict=True)):
                        continue
                    chan = self.channels.get(name)
                    if chan is None:
                        chan = self.channels[name] = Channel(name, name, "discovered", secret, self.per_channel)
                        chan.found_at = time.time()
                    unk.msgs.remove(m)
                    unk.total -= 1
                    m.pop("raw", None)
                    sts, text = res
                    sender, sep, body = text.partition(": ")
                    m.update(sts=sts, sender=sender if sep else "", text=body if sep else text)
                    self._file(chan, m)
                    self.by_packet[m["ph"]] = (chan.key, m)
                    break
            if not unk.msgs:
                del self.channels[key]
        for c in self.channels.values():
            c.msgs = deque(sorted(c.msgs, key=lambda x: x["t"]), maxlen=self.per_channel)

    def prune(self, now: float) -> None:
        cutoff = now - 3 * 24 * HOUR
        self.seen_ids = {k: v for k, v in self.seen_ids.items() if v >= cutoff}
        self.by_packet = {k: v for k, v in self.by_packet.items() if v[1]["t"] >= cutoff}

    # ---- output ----
    def view(self, now: float) -> dict:
        chans = []
        for c in self.channels.values():
            recent = [m for m in c.msgs if m["t"] >= now - 24 * HOUR]
            bins = [0] * 24
            for m in recent:
                bins[min(23, int((now - m["t"]) // HOUR))] += 1
            senders = {m.get("sender") for m in recent if m.get("sender")}
            snrs = [m["snr"] for m in recent if m.get("snr") is not None]
            hops = [m["hops"] for m in recent if m.get("hops") is not None]
            chans.append({
                "key": c.key, "name": c.name, "kind": c.kind, "total": c.total, "first": c.first, "last": c.last,
                "found_at": c.found_at, "count24": len(recent), "senders24": len(senders),
                "snr24": round(sum(snrs) / len(snrs), 1) if snrs else None,
                "hops24": round(sum(hops) / len(hops), 1) if hops else None,
                "hourly": bins[::-1],  # oldest hour first
                "msgs": [{k: v for k, v in m.items() if k != "raw"} for m in reversed(c.msgs)],
            })
        chans.sort(key=lambda x: -(x["last"] or 0))
        return {"updated": now, "channels": chans, "guesses": len(self.guesses)}

    def to_state(self) -> dict:
        return {"channels": [c.to_state() for c in self.channels.values()],
                "seen_ids": self.seen_ids, "learned": sorted(self.learned),
                "submitted": sorted(self.submitted)}

    def load_state(self, d: dict) -> None:
        for cd in d.get("channels", []):
            c = Channel.from_state(cd, self.per_channel)
            self.channels[c.key] = c
            for m in c.msgs:
                self.by_packet[m["ph"]] = (c.key, m)
        self.seen_ids = {int(k): v for k, v in d.get("seen_ids", {}).items()}
        self.learned = set(d.get("learned", []))
        self.submitted = set(d.get("submitted", []))
