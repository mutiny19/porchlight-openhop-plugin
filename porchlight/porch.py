"""The porch: Porchlight's own voice on the mesh (install, paste a token, light the porch, done).

"Light the porch" does every setup step from what openHop already knows:
  1. the repeater's location (openHop config) -> its general area, from one OpenStreetMap lookup (editable);
  2. its own companion identity, named just "Porchlight", bound to 127.0.0.1 on a free port;
  3. #porchlight in that companion's channel slot 1 (slot 0 is always Public in openHop).
After that the porch talks ONLY in #porchlight. The channel is a constant here, not a setting. Before every post the
slot's key is read back over the companion's frame port; if it isn't sha256("#porchlight")[:16] (someone changed the
slot), the porch goes dark and stays dark until its owner lights it again. It never sends DMs.
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .companion import FrameClient
from .core import hashtag_secret

log = logging.getLogger("porchlight")

CHANNEL = "#porchlight"  # the only place the porch ever speaks
SECRET = hashtag_secret(CHANNEL)
SLOT = 1
NODE_NAME = "Porchlight"
IDENTITY = "porchlight"  # openHop identity label of our companion
PORTS = range(5060, 5100)
# "<node name>: <text>" per channel packet. 91 keeps the packet at 99 B: longer posts (131-163 B) often reached no
# observer at all on a busy relay while 51-115 B ones reached 6-9 (was 160).
PACKET_TEXT = 91
UA = "porchlight-openhop-plugin (+https://github.com/mutiny19/porchlight-openhop-plugin)"


class PorchError(RuntimeError):
    pass


class Api:
    """openHop's REST API with the plugin's API token (X-API-Key)."""

    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None, **q):
        url = self.base + path + (("?" + urllib.parse.urlencode(q)) if q else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-API-Key": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
        if isinstance(out, dict) and out.get("success") is False:
            raise PorchError(out.get("error") or f"{path} failed")
        return out.get("data", out) if isinstance(out, dict) else out


def port_in_use(port: int) -> bool:
    """Something already listens on 127.0.0.1:port. openHop's delete leaves a deleted companion's frame server
    running until openHop restarts, so the config alone doesn't say which ports are free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def area_name(lat: float, lon: float) -> str:
    """'Springfield, IL' from one OpenStreetMap (Nominatim) reverse lookup. The position is rounded to ~1 km first."""
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.2f}", "lon": f"{lon:.2f}", "zoom": 10,
                                "addressdetails": 1, "accept-language": "en"})
    req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        a = (json.load(r) or {}).get("address") or {}
    place = next((a[k] for k in ("city", "town", "village", "hamlet", "municipality", "suburb", "county") if a.get(k)), "")
    region = (a.get("ISO3166-2-lvl4") or "").split("-")[-1] if a.get("country_code") == "us" else (a.get("country_code") or "").upper()
    return ", ".join(x for x in (place, region) if x)[:40]


class Porch:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "porch.json"
        self.s: dict = {"lit": False, "reason": "Not lit yet: press Light the porch", "area": "", "steps": []}
        try:
            self.s.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass
        self.api: Api | None = None
        self.cfg: dict = {}
        self.send_lock = threading.Lock()
        self.sent: list = []  # [(t, text)] our own posts, so we know them when they come back on air
        self.last_check = 0.0
        self.last_post = 0.0  # when we last transmitted; quiet_air waits for this too

    # ---- state ----
    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, separators=(",", ":")))
        os.replace(tmp, self.path)

    def configure(self, cfg: dict, api: Api | None) -> None:
        self.cfg, self.api = cfg, api

    @property
    def lit(self) -> bool:
        return bool(self.s.get("lit"))

    @property
    def area(self) -> str:
        return str(self.cfg.get("area") or "").strip() or self.s.get("area", "")

    def view(self) -> dict:
        return {k: self.s.get(k) for k in ("lit", "reason", "area", "auto_area", "lat", "lon", "port", "lit_at", "steps", "done_request")} | {"area": self.area}

    def go_dark(self, reason: str) -> None:
        if self.s.get("lit") or self.s.get("reason") != reason:
            log.warning("porch dark: %s", reason)
        self.s.update(lit=False, reason=reason)
        self._save()

    # ---- lighting ----
    def light(self) -> None:
        """Every setup step; the outcome of each lands in porch.json `steps` for the page."""
        steps = []
        self.s["steps"] = steps

        def step(text: str, ok: bool = True):
            steps.append({"text": text, "ok": ok})
            log.info("light the porch: %s%s", "" if ok else "FAILED: ", text)

        try:
            if not self.api or not self.api.token:
                raise PorchError("Add an openHop API token first")
            cfg = (self.api.call("GET", "/api/stats") or {}).get("config") or {}
            rep = cfg.get("repeater") or {}
            lat, lon = float(rep.get("latitude") or 0), float(rep.get("longitude") or 0)
            if lat or lon:
                step(f"Repeater location {lat:.3f}, {lon:.3f}")
                moved = math.dist((lat, lon), (self.s.get("lat") or 0, self.s.get("lon") or 0)) > 0.05
                if moved or not self.s.get("auto_area"):
                    try:
                        self.s["auto_area"] = area_name(lat, lon)
                    except Exception as e:
                        step(f"Couldn't look up the area name ({e}); type one in", ok=False)
                self.s.update(lat=lat, lon=lon, area=self.s.get("auto_area", ""))
            elif not self.area:  # no location is fine as long as the owner typed an area
                raise PorchError("The repeater has no location: type an area name (like Springfield, IL), "
                                 "or set the location in openHop's settings")
            if not self.area:
                raise PorchError("No area name: type one in (like Springfield, IL)")
            step(f"Area: {self.area}")
            port = self._companion()
            step(f"Companion '{NODE_NAME}' ready on 127.0.0.1:{port}")
            with FrameClient(port) as fc:
                fc.set_channel(SLOT, CHANNEL, SECRET)
                name, secret = fc.get_channel(SLOT)
                self._match_path_mode(fc)
            if secret != SECRET:
                raise PorchError("The companion didn't keep the #porchlight channel")
            step(f"{CHANNEL} set in channel slot {SLOT}")
            self.s.update(lit=True, reason="Lit", lit_at=time.time(), port=port)
            step(f"Porchlight is lit near {self.area}")
        except Exception as e:
            step(str(e), ok=False)
            self.s.update(lit=False, reason=str(e))
        self._save()

    def _match_path_mode(self, fc) -> None:
        """A new companion starts on 1-byte path hashes whatever the repeater uses, so copy the repeater's own setting.
        Never fatal: the porch works either way, and this repeater's mesh may need the setting it already has."""
        try:
            stats = self.api.call("GET", "/api/stats") or {}
            mode = ((stats.get("config") or {}).get("mesh") or {}).get("path_hash_mode")
            if mode in (0, 1, 2):
                fc.set_path_hash_mode(mode)
        except Exception as e:
            log.warning("couldn't match the repeater's path hash mode: %s", e)

    def running(self) -> bool:
        """openHop knows our companion by name, which is how posts are sent. A companion openHop saved but couldn't
        start (its port was taken) is in the config but not here."""
        try:
            self.api.call("GET", "/api/companion/self_info", companion_name=IDENTITY)
            return True
        except Exception:
            return False

    def _companion(self) -> int:
        """Our companion's frame port, creating the companion if this repeater doesn't have it running yet."""
        comps = (self.api.call("GET", "/api/identities") or {}).get("configured_companions") or []
        mine = next((c for c in comps if c.get("name") == IDENTITY), None)
        if mine is not None and self.running():
            if (mine.get("settings") or {}).get("node_name") != NODE_NAME:
                self.api.call("POST", "/api/companion/set_advert_name", {"advert_name": NODE_NAME, "companion_name": IDENTITY})
            return int((mine.get("settings") or {}).get("tcp_port"))
        # Until openHop restarts, a deleted companion keeps its port open and its one-byte key prefix reserved, so a
        # new companion can fail to start. openHop then saves it anyway; replace it with a fresh key and port and try
        # again (a new random key almost never repeats the prefix).
        for attempt in range(3):
            if mine is not None:  # saved but not running: replace it
                self.api.call("DELETE", "/api/delete_identity", name=IDENTITY, type="companion")
                comps = [c for c in comps if c is not mine]
            used = {int((c.get("settings") or {}).get("tcp_port") or 0) for c in comps}
            port = next((p for p in PORTS if p not in used and not port_in_use(p)), None)
            if port is None:
                raise PorchError("No free companion port between 5060 and 5099")
            settings = {"node_name": NODE_NAME, "tcp_port": port, "bind_address": "127.0.0.1"}
            self.api.call("POST", "/api/create_identity", {"name": IDENTITY, "type": "companion", "settings": settings})
            for _ in range(5):  # openHop starts a new companion right away; give it a moment
                if self.running():
                    return port
                time.sleep(1)
            log.info("companion didn't start on port %d (attempt %d); replacing it", port, attempt + 1)
            mine = {"name": IDENTITY, "settings": settings}
        raise PorchError("openHop saved the companion but couldn't start it. Restart openHop, then press "
                         "Light the porch again")

    def remove(self) -> None:
        """Turn off and delete the companion Light the porch created, so nothing of Porchlight is left in openHop's
        config (do this before uninstalling). Lighting again later creates a fresh one."""
        self.go_dark("Turned off and its companion removed")
        try:
            if not self.api:
                raise PorchError("no openHop API token")
            self.api.call("DELETE", "/api/delete_identity", name=IDENTITY, type="companion")
            self.s.update(steps=[{"text": f"Companion '{NODE_NAME}' removed from openHop", "ok": True}], port=None)
        except Exception as e:
            self.s["steps"] = [{"text": f"Couldn't remove the companion: {e}", "ok": False}]
        self._save()

    # ---- the guard ----
    def check(self) -> bool:
        """Is the porch still lit and its slot still #porchlight? Otherwise go dark."""
        if not self.lit or not self.s.get("port"):
            return False
        try:
            with FrameClient(self.s["port"]) as fc:
                name, secret = fc.get_channel(SLOT)
        except Exception as e:
            log.warning("porch check failed: %s", e)  # openHop restarting: try again next time, stay lit
            return False
        self.last_check = time.time()
        if secret != SECRET:
            self.go_dark(f"Its channel was changed (slot {SLOT} is now '{name or 'empty'}'): light the porch again")
            return False
        return True

    # ---- speaking ----
    def budget(self, tag: str = "") -> int:
        return PACKET_TEXT - len(NODE_NAME.encode()) - 2 - len(tag.encode("utf-8"))

    def quiet_air(self, quiet_s: float, max_s: float) -> None:
        """Wait until nothing was heard on air for quiet_s (at most max_s): don't talk over a busy mesh. Our own last post
        counts too, so part 2 doesn't go out while a repeater is still relaying part 1."""
        end = time.time() + max_s
        while time.time() < end:
            try:
                pkts = self.api.call("GET", "/api/recent_packets", limit=5) or []
                heard = max((p.get("timestamp") or 0 for p in pkts if p.get("rssi")), default=0)
            except Exception:
                return
            heard = max(heard, self.last_post)
            if time.time() - heard >= quiet_s:
                return
            time.sleep(1)

    def say(self, text: str, quiet_s: float = 5.0, max_s: float = 20.0) -> bool:
        """Post text in #porchlight, and only there. One post at a time, rate-capped, guard checked right before."""
        text = text.strip()
        if not text:
            return False
        if len(text.encode("utf-8")) > self.budget():
            text = text.encode("utf-8")[:self.budget() - 3].decode("utf-8", "ignore") + "..."
        with self.send_lock:
            now = time.time()
            self.sent = [(t, x) for t, x in self.sent if now - t < 3600]
            cap = int(self.cfg.get("max_posts_per_hour", 30))
            if len(self.sent) >= cap:
                log.warning("porch: %d posts in the last hour, holding %r", cap, text[:40])
                return False
            self.quiet_air(quiet_s, max_s)
            if not self.check():
                return False
            try:
                out = self.api.call("POST", "/api/companion/send_channel_message",
                                    {"channel_idx": SLOT, "text": text, "companion_name": IDENTITY})
                ok = bool((out or {}).get("sent"))
            except Exception as e:
                log.warning("porch send failed: %s", e)
                ok = False
                if getattr(e, "code", None) == 404:  # openHop doesn't know our companion any more
                    self.go_dark("openHop lost Porchlight's companion: press Light the porch again")
            if ok:
                self.sent.append((time.time(), text))
                self.last_post = time.time()
            log.info("porch: %s (%s)", text, "ok" if ok else "FAILED")
            return ok

    def say_again(self, text: str, timestamp: int, quiet_s: float = 5.0, max_s: float = 30.0) -> bool:
        """Resend an earlier post as the identical packet: same text, same timestamp. Nodes that already got it drop it
        as a duplicate, so it never shows twice; only nodes that missed it see it. Goes over the frame port because
        openHop's REST send can't set a timestamp."""
        with self.send_lock:
            self.quiet_air(quiet_s, max_s)
            if not self.check():
                return False
            try:
                with FrameClient(self.s["port"]) as fc:
                    fc.send_channel_text(SLOT, text, timestamp)
                ok = True
            except Exception as e:
                log.warning("porch resend failed: %s", e)
                ok = False
            if ok:
                self.last_post = time.time()
            log.info("porch: resent %s (%s)", text, "ok" if ok else "FAILED")
            return ok

    def observers_beyond(self, packet_hash: str):
        """How many observers other than this repeater's own heard packet_hash, per the CoreScope at reach_check_url.
        None when it can't tell: no URL set, no answer, or not even our own upload there yet (CoreScope behind)."""
        base = str(self.cfg.get("reach_check_url") or "").rstrip("/")
        own = str(self.cfg.get("reach_observer") or "")
        if not base or not own or not packet_hash:
            return None
        try:
            req = urllib.request.Request(f"{base}/api/packets/{packet_hash}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                obs = json.load(r).get("observations") or []
        except Exception:
            return None
        names = {o.get("observer_name") for o in obs}
        if own not in names:
            return None
        return len(names - {own})

    def is_mine(self, sender: str, text: str) -> bool:
        """Every porch is called Porchlight, so our own posts are known by what we sent."""
        if (sender or "").strip().lower() != NODE_NAME.lower():
            return False
        now = time.time()
        return any(x == text and now - t < 900 for t, x in list(self.sent))
