"""What the porch says in #porchlight, and when (all inside the plugin, so any openHop repeater can run a porch on its own).

- A knock (`porchlight`, `porch light` or `#porchlight` first in a message) gets an answer. A first-time visitor gets
  one welcoming message (they can't see the channel's history); a known visitor gets the busiest channels, the newest
  names and any region scopes heard. `porchlight add #x`, `porchlight regions`, `porchlight 2 of 3` work too.
- `help` (or `porchlight help`) says what Porchlight is, how to knock and how it learns names.
- 1-3 bare #names in a message are names sent in: kept, tried at once, answered.
- A channel that is news here (first heard, back after quiet_days, or just named and active) is announced "#name (Area)".
- Several porches hear each other: the one nearest the asker answers first and the rest stay quiet once they hear an
  answer; announcements wait a random while and are dropped if another porch posted the same one. A visitor counts as
  welcomed once any porch answered them.
Nobody gets an answer just for showing up: they have to knock.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path

from .core import HASHTAG_RE
from .porch import CHANNEL, NODE_NAME

log = logging.getLogger("porchlight")
DAY = 86400
WARMUP = 1800  # seconds after a fresh install before anything is announced
KNOCKS = ("porchlight", "#porchlight", "porch light")
ADD_WORDS = ("add", "name", "submit")
REGION_WORDS = ("region", "regions", "scope", "scopes")
NAME_RE = re.compile(r"#(?=[a-z0-9_-]*[a-z])[a-z0-9][a-z0-9_-]{0,30}")
PART_RE = re.compile(r"(\d{1,2})\s*(?:of|/)\s*(\d{1,2})", re.I)
# What other porches say (several can share #porchlight): an announcement "#name (Area)", an answer to a knock
# "@[who] Welcome!/Top channels/...", an answer to names sent in "@[who] #x added to/is already on Porchlight ...".
OTHER_ANNOUNCE_RE = re.compile(r"^(#[a-z0-9][a-z0-9_-]{0,40})\s*\(.*\)\s*$", re.I)
OTHER_REPLY_RE = re.compile(r"^@\[(.+?)\]\s+(?:\d+/\d+ )?(?:Welcome|Busiest|No hashtag channel traffic|Top regions|No region-scoped)", re.I)
OTHER_ADD_RE = re.compile(r"^@\[(.+?)\]\s+#\S+ (?:added to|is already on) Porchlight", re.I)
OTHER_HELP_RE = re.compile(r"^@\[(.+?)\]\s+(?:\d+/\d+ )?Porchlight shows", re.I)
HELP_RE = re.compile(r"^[!/]?help[!?.]*$", re.I)
REGION_WORD_RE = re.compile(r"\b(regions?|scopes?)\b", re.I)
SEP = " | "
LABEL = len("9/9 ")
NEW_REGION_DAYS = 14


class Speaker:
    def __init__(self, data_dir: Path, porch, store, view_fn):
        self.porch, self.store, self.view_fn = porch, store, view_fn
        self.path = data_dir / "speaker.json"
        self.cfg: dict = {}
        self.started = time.time()
        self.lock = threading.RLock()  # speaker.json state is touched from the main thread and answer threads
        self.st = {"greeted": {}, "announced": {}}
        try:
            self.st.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass
        # When this install began. Its first scan names many channels at once from the backfill and the name lists;
        # none of that is news, so nothing is announced until WARMUP after this.
        first_run = "since" not in self.st
        self.st.setdefault("since", self.started)
        self.replied: dict = {}      # visitor (lower) -> when another porch answered their knock
        self.replied_add: dict = {}  # visitor (lower) -> when another porch answered their names
        self.heard_ann: dict = {}    # "#name" -> when another porch announced it
        self.pending: set = set()
        self.knocked: dict = {}      # visitor (lower) -> last knock we acted on
        self.adds: dict = {}         # visitor (lower) -> [times] of names sent in
        self.sent_parts: dict = {}   # visitor (lower) -> (t, [parts]) of the last multi-part answer
        self.regions_at = 0.0
        if first_run:
            self._save()

    def configure(self, cfg: dict) -> None:
        self.cfg = dict(cfg)
        tags = lambda v: ["#" + str(x).strip().lstrip("#").lower() for x in (v or []) if str(x).strip().lstrip("#")]
        self.cfg["auto_channels"] = tags(cfg.get("auto_channels", ["#wardriving"]))
        self.cfg["local_repeaters"] = [str(x).strip().upper() for x in cfg.get("local_repeaters") or [] if str(x).strip()]

    def c(self, key, default):
        v = self.cfg.get(key)
        return default if v is None or v == "" else v

    @property
    def top_hours(self) -> int:
        return int(self.c("top_hours", 72))  # top channels active in the last 72 h

    @property
    def area(self) -> str:
        return self.porch.area

    def _save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.st, separators=(",", ":")))
            os.replace(tmp, self.path)

    def _spawn(self, fn, *args) -> None:
        threading.Thread(target=self._guarded, args=(fn, *args), daemon=True).start()

    @staticmethod
    def _guarded(fn, *args):
        try:
            fn(*args)
        except Exception as e:
            log.warning("speaker: %s failed: %s", fn.__name__, e)

    # ---- hearing #porchlight (main thread) ----
    def hear(self, m: dict) -> None:
        """A newly heard #porchlight message (store.add filed it)."""
        sender, text = (m.get("sender") or "").strip(), (m.get("text") or "").strip()
        if not text or self.porch.is_mine(sender, text):
            return
        self._note_other(text)
        if not self.porch.lit or m.get("t", 0) < self.started - 5 or time.time() - m.get("t", 0) > 120:
            return  # dark, or old traffic from the backfill: never answer it
        if sender.lower() == NODE_NAME.lower() or sender.lower() in {n.lower() for n in self.c("ignore_names", [])}:
            return  # other porches and bots don't knock
        hops = m.get("hops") or 0
        body = re.sub(r"^@\[porchlight\]\s*", "", text, flags=re.I)  # "@[Porchlight] porchlight" is a knock too
        if body.startswith("@["):
            return  # talking to someone else
        low = body.lower()
        if HELP_RE.match(low):
            self._knock(sender, "help", hops)
            return
        knock = next((k for k in sorted(KNOCKS, key=len, reverse=True)
                      if low == k or low.startswith(k + " ") or re.match(re.escape(k) + r"[!?.]+$", low)), None)
        if knock:
            self._knock(sender, body[len(knock):].strip(" !?."), hops)
            return
        names = [n for n in self.bare_names(body) if n != CHANNEL]
        if names:
            self._names(sender, names, hops)
            return
        if REGION_WORD_RE.search(body) and time.time() - self.regions_at >= 120:
            self.regions_at = time.time()
            self._spawn(self._answer, sender, hops, "regions")

    def _note_other(self, text: str) -> None:
        now = time.time()
        m = OTHER_REPLY_RE.match(text)
        if m:
            self.replied[m.group(1).strip().lower()] = now
            self.mark_greeted(m.group(1))
            return
        m = OTHER_ADD_RE.match(text)
        if m:
            self.replied_add[m.group(1).strip().lower()] = now
            return
        m = OTHER_HELP_RE.match(text)
        if m:  # answered, but help isn't a welcome: their first knock still gets one
            self.replied[m.group(1).strip().lower()] = now
            return
        m = OTHER_ANNOUNCE_RE.match(text)
        if m:
            key = m.group(1).lower()
            self.heard_ann[key] = now
            if key not in self.pending:
                with self.lock:
                    self.st["announced"][key] = max(self.st["announced"].get(key, 0), now)
                self._save()

    def _knock(self, sender: str, args: str, hops: int) -> None:
        who = sender.lower()
        words = args.split()
        if PART_RE.fullmatch(args or "-"):
            i, n = map(int, PART_RE.fullmatch(args).groups())
            rec = self.sent_parts.get(who)
            if rec and time.time() - rec[0] <= 3600 and len(rec[1]) == n and 1 <= i <= n:
                self._spawn(self._send_parts, [rec[1][i - 1]], sender, False)
            return
        if words and words[0].lower() in ADD_WORDS:
            names = []
            for w in words[1:]:
                n = "#" + w.strip(".,!?;:()[]'\"").lstrip("#").lower()
                if NAME_RE.fullmatch(n) and n not in names and n != CHANNEL:
                    names.append(n)
            if names:
                self._names(sender, names[:3], hops)
            return
        first = words[0].lower().strip("!?.") if words else ""
        kind = "regions" if first in REGION_WORDS else "help" if first == "help" else "knock"
        if time.time() - self.knocked.get((who, kind), 0) < 60:
            return  # the same ask twice in a minute: one answer is enough (help then a knock still get both)
        self.knocked[(who, kind)] = time.time()
        self._spawn(self._answer, sender, hops, kind)

    def _names(self, sender: str, names: list, hops: int) -> None:
        who, now = sender.lower(), time.time()
        recent = [t for t in self.adds.get(who, []) if now - t < 3600]
        if len(recent) + len(names) > 10:
            log.info("speaker: %s sent over 10 names this hour, ignoring %s", sender, names)
            return
        self.adds[who] = recent + [now] * len(names)
        results = [self.store.submit(n, now) for n in names]  # every porch keeps the names; one answers
        self.store.dirty = True
        log.info("names sent in by %s: %s", sender[:40], ", ".join(f"{r['name']} {r['result']}" for r in results))
        self._spawn(self._answer_names, sender, hops, names, results)

    # ---- answering (own threads) ----
    def _wait_turn(self, hops: int, done) -> bool:
        """The nearer the asker, the sooner we answer; False if another porch answered meanwhile."""
        time.sleep(min(hops, 8) * float(self.c("reply_hop_seconds", 5)) + random.uniform(0, float(self.c("reply_jitter_seconds", 4))))
        return not done()

    def _answered(self, who: str) -> bool:
        return time.time() - self.replied.get(who.lower(), 0) <= 120

    def _answer(self, sender: str, hops: int, kind: str) -> None:
        if not self._wait_turn(hops, lambda: self._answered(sender)):
            log.info("speaker: another porch answered %s, staying quiet", sender)
            return
        tag = f"@[{sender}] "
        budget = self.porch.budget(tag)
        if kind == "help":
            segs = [self.help_text(budget)]
        elif kind == "regions":
            segs = self.regions_fitted(budget) or [self.no_regions_text()]
        else:
            # Top channels and new channels on every call, two messages is fine
            cap = budget - LABEL
            newest = self.newest_text(int(self.c("newest_count", 4)), cap)
            room = cap if newest else budget
            if self.is_new_visitor(sender):
                segs = [self.welcome_text(room), newest]
            else:
                top = self.top_text(room) or f"No hashtag channel traffic heard here in {self.span()}."
                segs = [top, newest] + self.region_segments(new=False)
        hinted = "#name" in segs[0]  # a welcome that kept its own hint
        parts = self.pack(segs, budget, hint=kind == "regions" or kind == "knock" and not hinted)
        if len(parts) > 1:
            self.sent_parts[sender.lower()] = (time.time(), parts)
        if self._send_parts(parts, sender, True) and kind != "help":
            self.mark_greeted(sender)

    def _answer_names(self, sender: str, hops: int, names: list, results: list) -> None:
        done = lambda: time.time() - self.replied_add.get(sender.lower(), 0) <= 120
        if not self._wait_turn(hops, done):
            log.info("speaker: another porch answered %s's names, staying quiet", sender)
            return
        text = self.results_text(names, results)
        if text and not done():
            self._post(f"@[{sender}] {text}", sender)

    def _send_parts(self, parts: list, sender: str, check_others: bool) -> bool:
        ok_all = True
        for i, p in enumerate(parts):
            if i == 0 and check_others and self._answered(sender):
                log.info("speaker: another porch answered %s while we waited for quiet air", sender)
                return True
            # Later parts wait part_gap_s after the previous one (at 8 s, part 2 kept colliding
            # with the next repeaters' relays of part 1 and was lost; part 1 got out)
            gap = float(self.c("part_gap_s", 20))
            ok_all = self._post(f"@[{sender}] {p}", sender, quiet=5.0 if i == 0 else gap) and ok_all
        return ok_all

    def _post(self, text: str, sender: str = "", quiet: float = 5.0) -> bool:
        ok = self.porch.say(text, quiet_s=quiet, max_s=30.0)
        if ok and sender:
            threading.Timer(20.0, self._guarded, args=(self._retry, text, sender, time.time())).start()
        return ok

    def _retry(self, text: str, sender: str, sent_at: float) -> None:
        """Resend an answer once if no repeater beyond our own was heard passing it on (it never got out) and no other
        porch answered that person meanwhile. Announcements are never resent: nobody is waiting for them."""
        if self._answered(sender) or time.time() - self.replied_add.get(sender.lower(), 0) <= 120:
            return
        view = self.view_fn() or {}
        chan = next((c for c in view.get("channels", []) if c.get("key") == CHANNEL), None)
        mine = [m for m in (chan or {}).get("msgs", []) if (m.get("sender") or "").lower() == NODE_NAME.lower()
                and m.get("text") == text and abs((m.get("t") or 0) - sent_at) < 20]
        if not mine:
            return  # not in the log yet: can't tell, so don't double-post
        local = self.c("local_repeaters", [])
        m = mine[0]
        if local and "vias" in m:
            beyond = sum(1 for path in m["vias"] if any(not any(k.startswith(h.upper()) or h.upper().startswith(k) for k in local) for h in path))
        else:
            beyond = (m.get("heard") or 1) - 1
        if beyond == 0:
            # We often can't hear the relay that carried it, so ask a CoreScope first (if configured)
            heard = self.porch.observers_beyond(m.get("ph"))
            if heard:
                log.info("speaker: %r reached %d observer(s) per CoreScope, no resend", text[:40], heard)
            elif heard == 0 or not m.get("sts"):
                log.info("speaker: nobody heard %r, posting it again", text[:40])
                self.porch.say(text, quiet_s=5.0, max_s=30.0)  # fresh packet: a relay that dropped it won't block it
            else:
                log.info("speaker: no repeat heard for %r, resending the identical packet", text[:40])
                self.porch.say_again(m.get("text") or text, int(m["sts"]))  # can never show twice

    # ---- news (main thread, after each poll) ----
    def consider_news(self) -> None:
        if not self.porch.lit:
            return
        now, fresh = time.time(), 3600
        quiet, auto = float(self.c("quiet_days", 7)) * DAY, set(self.c("auto_channels", ["#wardriving"]))
        posted = 0
        for c in list(self.store.channels.values()):
            name = c.key
            if c.kind not in ("hashtag", "discovered") or name == CHANNEL or name in auto or not HASHTAG_RE.fullmatch(name):
                continue
            skip = {NODE_NAME.lower()} | {str(n).lower() for n in self.c("ignore_names", [])}
            times = sorted((m["t"] for m in c.msgs if (m.get("sender") or "").lower() not in skip), reverse=True)
            if not times or times[0] < self.started - fresh:
                continue
            newest = times[0]
            # News only: first message heard here, first after quiet_days of silence, or a channel just named that
            # had traffic in the last day.
            warm = float(self.st.get("since") or 0) + WARMUP
            fresh_msg = newest >= warm and now - newest <= fresh and (len(times) == 1 or newest - times[1] >= quiet)
            fresh_name = c.found_at and c.found_at >= warm and now - c.found_at <= fresh and now - newest <= DAY
            if not (fresh_msg or fresh_name) or newest < self.st["announced"].get(name, 0) + DAY or name in self.pending:
                continue
            if posted >= 3:
                break
            self.pending.add(name)
            with self.lock:
                self.st["announced"][name] = now
            self._spawn(self._announce, name, now)
            posted += 1

    def _announce(self, name: str, claimed: float) -> None:
        try:
            time.sleep(random.uniform(float(self.c("announce_delay_min", 10)), float(self.c("announce_delay_max", 90))))
            if self.heard_ann.get(name, 0) >= claimed:
                log.info("speaker: %s skipped, another porch announced it", name)
                return
            if self.area:
                self._post(f"{name} ({self.area})")
            else:
                self._post(name)
        finally:
            self.pending.discard(name)
            self._save()

    # ---- visitors ----
    def is_new_visitor(self, name: str) -> bool:
        return time.time() - self.st["greeted"].get((name or "").lower(), 0) > float(self.c("greet_days", 180)) * DAY

    def mark_greeted(self, name: str) -> None:
        with self.lock:
            g = self.st["greeted"]
            g[(name or "").strip().lower()] = time.time()
            if len(g) > 2000:
                for k, _ in sorted(g.items(), key=lambda kv: kv[1])[:len(g) - 2000]:
                    del g[k]
        self._save()

    # ---- texts ----
    @staticmethod
    def bare_names(body: str) -> list:
        words = body.replace(",", " ").split()
        if not 1 <= len(words) <= 3:
            return []
        names = []
        for w in words:
            n = w.strip(".!?;:").lower()
            if not NAME_RE.fullmatch(n):
                return []
            if n not in names:
                names.append(n)
        return names

    def help_text(self, budget: int) -> str:
        """What Porchlight is (the answer comes from the nearest Porchlight install, which says where it is), how to
        knock, where its names come from. The longest wording that fits one message. 'Porchlight
        shows' starts it so other porches recognise the answer (OTHER_HELP_RE)."""
        # One 99 B packet, plain words. How to use it matters more than where it is, so the area
        # goes before "Send porchlight" does.
        heads = ([f"Porchlight shows busy channels near {self.area}."] if self.area else []) + \
            ["Porchlight shows busy channels nearby.", "Porchlight shows channels."]  # the last one fits a 31-byte name
        # the richest description that still leaves room for at least "Send porchlight"
        for head in heads:
            for tail in (" Send porchlight to list them. Post #name to add one", " Send porchlight to list them",
                         " Send porchlight"):
                if len((head + tail).encode()) <= budget:
                    return head + tail
        return heads[-1]

    def results_text(self, names: list, results: list) -> str:
        where = f"Porchlight {self.area}" if self.area else "Porchlight"
        parts = []
        for r in results:
            n, res = r.get("name"), r.get("result")
            if res == "named":
                parts.append(f"{n} added to {where}, found {r.get('total', 0)} msgs here!")
            elif res == "known":
                parts.append(f"{n} is already on {where}")
            elif res == "saved":
                parts.append(f"{n} added to {where}")
        return "; ".join(parts)

    def _rows(self) -> list:
        """(name, msgs in top_hours, buffer full, automated) for the busiest hashtag channels of the last top_hours, automated
        ones (auto_channels) after them whatever their count."""
        view, now = self.view_fn() or {}, time.time()
        since = now - self.top_hours * 3600
        auto, rows = set(self.c("auto_channels", ["#wardriving"])), []
        skip = {NODE_NAME.lower()} | {str(n).lower() for n in self.c("ignore_names", [])}  # bots don't make a channel busy
        for c in view.get("channels", []):
            name = (c.get("name") or "").lower()
            if c.get("kind") not in ("hashtag", "discovered") or name == CHANNEL or not HASHTAG_RE.fullmatch(name):
                continue
            msgs = c.get("msgs", [])
            recent = [m for m in msgs if (m.get("t") or 0) >= since and (m.get("sender") or "").lower() not in skip]
            if not recent:
                continue
            full = len(msgs) >= 100 and min(m.get("t") or 0 for m in msgs) >= since
            senders = {m.get("sender") for m in recent if m.get("sender")}
            rows.append((name in auto, -len(recent), -len(senders), name, full))
        rows.sort()
        rows = [(name, -n, full, a) for a, n, _, name, full in rows]
        top = int(self.c("top_count", 4))
        return [r for r in rows if not r[3]][:top] + [r for r in rows if r[3]]

    @staticmethod
    def _fit(head: str, items: list, max_bytes: int, tail: str = "") -> str:
        """head + items + tail within max_bytes, dropping automated channels first, then the quietest (never splitting one)."""
        items = list(items)
        while items:
            text = head + ", ".join(items) + tail
            if len(text.encode("utf-8")) <= max_bytes:
                return text
            # People channels matter most, so an automated "(bots)" channel goes first, then the quietest
            bots = [i for i, x in enumerate(items) if "(bots)" in x]
            items.pop(bots[-1] if bots else -1)
        return ""

    def span(self) -> str:
        """The look-back window in words people read at a glance: '3 days', '12h' (plain wording)."""
        h = self.top_hours
        return f"{h // 24} days" if h % 24 == 0 and h >= 48 else f"{h}h"

    def top_text(self, max_bytes: int) -> str:
        # "#echo 45" = messages in the window; automated channels are marked "(bots)" so nobody expects people there
        items = [f"{n} (bots)" if a else f"{n} {c}" for n, c, full, a in self._rows()]
        # Repeat visitors know where they are: no area here (the welcome and help name it); counts = msgs in the window
        return self._fit(f"Busiest, {self.span()}: ", items, max_bytes)

    def welcome_text(self, max_bytes: int) -> str:
        where = f" near {self.area}" if self.area else ""
        hint = str(self.c("welcome_hint", "Know another? Post #name here"))
        tail = f" | {hint}" if hint else ""
        items = [f"{n} (bots)" if a else n for n, _, _, a in self._rows()]
        head = f"Welcome! Active{where}: "
        for t in (tail, ""):  # the hint goes before any channel does
            text = head + ", ".join(items) + t
            if items and len(text.encode("utf-8")) <= max_bytes:
                return text
        text = self._fit(head, items, max_bytes)
        if text:
            return text
        return f"Welcome! No hashtag channels heard{where} in {self.span()}.{tail}"[:max_bytes]

    def newest_text(self, count: int, max_bytes: int) -> str:
        view, now = self.view_fn() or {}, time.time()
        days = float(self.c("newest_days", 7)) * DAY
        found = [(c["found_at"], c["name"]) for c in view.get("channels", [])
                 if c.get("kind") == "discovered" and c.get("found_at") and c.get("name") != CHANNEL
                 and now - c["found_at"] <= days and now - (c.get("last") or 0) <= days]
        items = [n for _, n in sorted(found, reverse=True)[:count]]
        while items:
            text = f"New in {round(days / DAY)} days: " + ", ".join(items)
            if len(text.encode()) <= max_bytes:
                return text
            items.pop()
        return ""

    def region_segments(self, new_max: int = 70, new: bool = True) -> list:
        r = (self.view_fn() or {}).get("regions") or {}
        if not r:
            return []
        hours = round(r.get("window_days", 3) * 24)
        top = ", ".join(f"{x['name']} {x['count']}" for x in r.get("top", [])[:5])
        other = f" (+{r['unknown']} unknown)" if r.get("unknown") else ""
        segs = [f"Top regions {hours}h: {top or 'none we carry'}{other}"] if top or other else []
        if not new:
            return segs
        fresh = [x["name"] for x in r.get("new_heard", []) if time.time() - (x.get("first") or 0) <= NEW_REGION_DAYS * DAY]
        shown = list(fresh)
        while shown:
            more = len(fresh) - len(shown)
            t = "New regions: " + ", ".join(shown) + (f" +{more}" if more else "")
            if len(t.encode()) <= new_max:
                segs.append(t)
                break
            shown.pop()
        return segs

    def regions_fitted(self, budget: int) -> list:
        first = self.region_segments(new=False)
        room = budget - sum(len(s.encode()) for s in first) - len(SEP)
        return self.region_segments(new_max=room) if room >= 30 else self.region_segments()

    def no_regions_text(self) -> str:
        r = (self.view_fn() or {}).get("regions") or {}
        return f"No region-scoped traffic heard here in {round(r.get('window_days', 3) * 24)}h"

    def pack(self, segs: list, budget: int, hint: bool = False) -> list:
        """As few messages as the segments fit in, joined with ' | '; 2+ get numbered plus a 'Missed one?' segment.
        hint=True adds the how-to line when it fits in the last message."""
        segs = [s for s in segs if s]
        short_hint = str(self.c("short_hint", "Know a channel? Post #name here"))

        def pieces(s, b):
            """A segment too long for one message, cut at ', ' (then spaces) into pieces that fit (smaller
            packets; cutting it off with '...' would drop channels)."""
            out = []
            while len(s.encode()) > b:
                n = len(s)
                while n > 1 and len(s[:n].encode()) > b:
                    n -= 1
                cut = max(s.rfind(", ", 0, n), s.rfind(" ", 0, n) if s.rfind(", ", 0, n) < n // 2 else -1)
                if cut < n // 3:
                    cut = n
                out.append(s[:cut].rstrip(", "))
                s = s[cut:].lstrip(", ")
            return out + ([s] if s else [])

        def fill(items, b):
            parts, cur = [], ""
            for s in [p for item in items for p in pieces(item, b)]:
                cand = s if not cur else cur + SEP + s
                if len(cand.encode()) <= b:
                    cur = cand
                else:
                    parts.append(cur)
                    cur = s
            return parts + ([cur] if cur else [])

        def with_hint(parts, b):
            last = parts[-1] + SEP + short_hint if parts and hint and short_hint else None
            if last and len(last.encode()) <= b:
                parts[-1] = last
            return parts

        parts = fill(segs, budget)
        if len(parts) <= 1:
            return with_hint(parts, budget)
        # Short "1/2" labels and no "Missed one?" segment; with 99 B packets every byte goes to channels.
        # 'porchlight 2 of 2' still re-sends one part for anyone who asks.
        parts = with_hint(fill(segs, budget - LABEL), budget - LABEL)
        n = len(parts)
        return [f"{i}/{n} {p}" for i, p in enumerate(parts, 1)]
