"""Region scopes heard by this repeater: which region each scoped flood belongs to, for `porchlight regions`.

A scoped packet (route TRANSPORT_FLOOD 0 / TRANSPORT_DIRECT 3) carries a 16-bit transport code =
HMAC-SHA256(region key, payload type byte + payload)[:2] (MeshCore TransportKey::calcTransportCode; 0000/FFFF reserved).
The region key is sha256("#name")[:16], the same key openHop stores, so the regions this repeater carries can be matched.
openHop's packet list leaves out the raw bytes, so each new scoped packet is looked up once by hash and cached in
data/regions.json. Only packets heard on air count (rssi != 0); our own transmissions don't.

Unknown regions: a scoped packet that matches none of our regions is KEPT
(transport code, payload type, payload, signal, path) for a year in regions.json `unknown`, so it can be tested later
against names a group publishes. Nothing is guessed here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request

log = logging.getLogger("porchlight")
SCOPED_ROUTES = (0, 3)
UNKNOWN = "?"
KEEP_UNKNOWN_S = 365 * 86400
MAX_UNKNOWN = 500


def transport_code(key: bytes, ptype: int, payload: bytes) -> int:
    v = int.from_bytes(hmac.new(key, bytes([ptype]) + payload, hashlib.sha256).digest()[:2], "little")
    return 1 if v == 0 else (0xFFFE if v == 0xFFFF else v)


class Regions:
    def __init__(self, path, window_days: float = 3.0):
        self.path = path
        self.window = window_days * 86400
        self.hits: dict = {}  # packet_hash -> [t, region name or "?"]
        self.keys: list = []  # [{name, created_at}] carried by this repeater
        self.dirty = False
        self.last_run = 0.0
        self.unknown: dict = {}  # packet_hash -> {t, code, type, payload, rssi, snr, path} (kept a year)
        self.first_heard: dict = {}  # region name -> when we first heard it on air (kept for good)
        try:
            d = json.loads(path.read_text())
            self.hits, self.unknown = d.get("hits", {}), d.get("unknown", {})
            self.first_heard = d.get("first_heard", {})
        except (OSError, ValueError):
            self.hits = {}
        for t, n in self.hits.values():  # seed from hits cached before 0.6.5
            if n != UNKNOWN and t < self.first_heard.get(n, float("inf")):
                self.first_heard[n] = t
        # Unknown hits from before 0.6.2 were counted without their details: look them up again.
        for h in [h for h, (_, n) in self.hits.items() if n == UNKNOWN and h not in self.unknown]:
            del self.hits[h]

    def _get(self, base: str, token: str, path: str, **q):
        url = f"{base}{path}" + ("?" + urllib.parse.urlencode(q) if q else "")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"X-API-Key": token}), timeout=20) as r:
            body = json.load(r)
        if not body.get("success", True):
            raise RuntimeError(body.get("error", "API error"))
        return body.get("data")

    def update(self, base: str, token: str, every_s: float = 300) -> bool:
        """Refresh at most every every_s; returns True when the view changed."""
        now = time.time()
        if now - self.last_run < every_s:
            return False
        self.last_run = now
        keys = self._get(base, token, "/api/transport_keys") or []
        self.keys = [{"name": (k.get("name") or "").lstrip("#"), "created_at": k.get("created_at") or 0,
                      "key": base64.b64decode(k["transport_key"])} for k in keys if k.get("transport_key")]
        start = now - self.window
        recs = []
        for route in SCOPED_ROUTES:
            recs += self._get(base, token, "/api/filtered_packets", route=route,
                              start_timestamp=f"{start:.3f}", limit=1000) or []
        changed = False
        for rec in recs:
            h = rec.get("packet_hash")
            if not h or h in self.hits or not rec.get("rssi"):
                continue
            try:
                full = self._get(base, token, "/api/packet_by_hash", packet_hash=h)
                full = full[0] if isinstance(full, list) else full
                raw = bytes.fromhex(full["raw_packet"])
                code = raw[1] | raw[2] << 8
                payload = bytes.fromhex(full["payload"])
                ptype = int(full["type"])
            except Exception as e:  # keep going; try this one again next time
                log.warning("region lookup %s failed: %s", h, e)
                continue
            name = next((k["name"] for k in self.keys if transport_code(k["key"], ptype, payload) == code), UNKNOWN)
            t = rec.get("timestamp") or now
            self.hits[h] = [t, name]
            if name != UNKNOWN and t < self.first_heard.get(name, float("inf")):
                self.first_heard[name] = t
            if name == UNKNOWN:
                self.unknown[h] = {"t": t, "code": f"{code:04x}", "type": ptype, "payload": full["payload"],
                                   "rssi": full.get("rssi"), "snr": full.get("snr"), "path": full.get("original_path")}
                log.info("unknown region: packet %s code %04x type %d", h, code, ptype)
            changed = True
        for h in [h for h, (t, _) in self.hits.items() if t < start]:
            del self.hits[h]
            changed = True
        old = sorted((u["t"], h) for h, u in self.unknown.items())
        for t, h in old[:max(0, len(old) - MAX_UNKNOWN)] + [x for x in old if x[0] < now - KEEP_UNKNOWN_S]:
            if self.unknown.pop(h, None) is not None:
                changed = True
        if changed:
            self.path.write_text(json.dumps({"hits": self.hits, "unknown": self.unknown, "first_heard": self.first_heard}, separators=(",", ":")))
        self.dirty = True
        return True

    def view(self) -> dict:
        counts: dict = {}
        for _, name in self.hits.values():
            counts[name] = counts.get(name, 0) + 1
        unknown = counts.pop(UNKNOWN, 0)
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        newest = sorted(self.keys, key=lambda k: -k["created_at"])
        return {"window_days": round(self.window / 86400, 1), "updated": self.last_run,
                "top": [{"name": n, "count": c} for n, c in top], "unknown": unknown,
                "carried": len(self.keys), "unknown_kept": len(self.unknown),
                "newest": [{"name": k["name"], "added": k["created_at"]} for k in newest[:200]],
                # "New regions" = regions first heard on air lately, newest first
                "new_heard": [{"name": n, "first": t} for n, t in sorted(self.first_heard.items(), key=lambda kv: -kv[1])][:50]}
