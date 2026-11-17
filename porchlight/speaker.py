"""What the porch says in #porchlight, and when (all inside the plugin, so any openHop repeater can run a porch on its own).

- A knock (`porchlight`, `porch light` or `#porchlight` first in a message, or just `knock` / `knock knock`) gets an answer. A first-time visitor gets
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
OTHER_ADD_RE = re.compile(r"^@\[(.+?)\]\s+(?:\d+/\d+ )?#\S+ (?:added to|is already on) Porchlight", re.I)
# A follow-up part ("2/3 ..."): only the first part of an answer carries @[who], so this is what the rest look like.
PART_RE_OUT = re.compile(r"^\d{1,2}/\d{1,2} ")
OTHER_HELP_RE = re.compile(r"^@\[(.+?)\]\s+(?:\d+/\d+ )?Porchlight shows", re.I)
HELP_RE = re.compile(r"^[!/]?help[!?.]*$", re.I)
# "knock" or "knock knock" as the whole message is a knock too ("knock it off" is not)
KNOCK_RE = re.compile(r"^knock(?:[\s,.!]+knock)?[\s!?.]*$", re.I)
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
        self.answer_lock = threading.Lock()  # one answer on air at a time: no interleaved parts, one cap check
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
        body = re.sub(r"^@\[porchlight\]\s*", "", text, flags=re.I)  # a reply to us ("@[Porchlight] porchlight") counts
        if self.looks_like_a_porch(body):
            return  # talking to someone else, or a porch under another name: porches never answer each other
        low = body.lower()
        if HELP_RE.match(low):
            self._knock(sender, "help", hops)
            return
        if KNOCK_RE.match(low):
            self._knock(sender, "", hops)
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

    @staticmethod
    def looks_like_a_porch(text: str) -> bool:
        """Porchlight's own kinds of message: an answer (@[who] ...), a follow-up part (2/3 ...) or an announcement
        (#name (Area))."""
        return bool(text.startswith("@[") or PART_RE_OUT.match(text) or OTHER_ANNOUNCE_RE.match(text))

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
                self._spawn(self._resend_part, rec[1][i - 1], sender)
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
        if kind == "help":
            segs, hint, sep = self.help_segments(), "", " "
        elif kind == "regions":
            segs, hint, sep = self.region_segments() or [self.no_regions_text()], self.short_hint(), SEP
        else:
            newest = self.newest_text(int(self.c("newest_count", 4)))
            if self.is_new_visitor(sender):
                segs, hint = [self.welcome_text(), newest], self.welcome_hint()
            else:
                top = self.top_text() or f"No hashtag channel traffic heard here in {self.span()}."
                segs, hint = [top, newest] + self.region_segments(new=False), self.short_hint()
            sep = SEP
        parts = self.pack(segs, self.porch.budget(f"@[{sender}] "), self.porch.budget(), hint=hint, sep=sep)
        if len(parts) > 1:
            self.sent_parts[sender.lower()] = (time.time(), parts)
        if self._send_parts(parts, sender, True) and kind != "help":
            self.mark_greeted(sender)

    def _answer_names(self, sender: str, hops: int, names: list, results: list) -> None:
        """Every porch that has heard traffic on one of the names answers: each says what its own repeater heard.
        Porches that heard none of them stay quiet, except that the nearest one still confirms the names were added
        when no porch has answered at all, so the sender always hears back."""
        heard_here = any(r.get("result") == "named" or (r.get("result") == "known" and r.get("total")) for r in results)
        done = (lambda: False) if heard_here else (lambda: time.time() - self.replied_add.get(sender.lower(), 0) <= 120)
        if not heard_here:  # give porches that heard the channel the first word; this one only confirms if none does
            # longer than the farthest answering porch waits (8 hops x 5 s + 4 s jitter + up to 30 s for quiet air)
            time.sleep(float(self.c("confirm_wait_s", 75)))
        if not self._wait_turn(hops, done):
            log.info("speaker: another porch answered %s's names and none was heard here, staying quiet", sender)
            return
        segs = self.results_segments(results)
        if segs and not done():
            parts = self.pack(segs, self.porch.budget(f"@[{sender}] "), self.porch.budget(), sep="; ")
            self._send_parts(parts, sender, False)

    def _send_parts(self, parts: list, sender: str, check_others: bool) -> bool:
        """An answer's parts in order. Only the first carries @[who] (other porches see who was answered from it);
        the rest follow right after with their 2/3 label."""
        with self.answer_lock:
            left = getattr(self.porch, "posts_left", lambda: len(parts))()
            if len(parts) > left:
                log.warning("speaker: answer to %s needs %d posts, the hourly cap leaves %d; not starting it", sender,
                            len(parts), left)
                return False
            return self._send_parts_now(parts, sender, check_others)

    def _send_parts_now(self, parts: list, sender: str, check_others: bool) -> bool:
        ok_all = True
        for i, p in enumerate(parts):
            if i == 0 and check_others and self._answered(sender):
                log.info("speaker: another porch answered %s while we waited for quiet air", sender)
                return True
            # Later parts wait part_gap_s after the previous one (at 8 s, part 2 kept colliding
            # with the next repeaters' relays of part 1 and was lost; part 1 got out)
            gap = float(self.c("part_gap_s", 20))
            text = f"@[{sender}] {p}" if i == 0 else p
            ok_all = self._post(text, sender, quiet=5.0 if i == 0 else gap) and ok_all
        return ok_all

    def _resend_part(self, part: str, sender: str) -> bool:
        """One part again, for `porchlight 2 of 3`: with @[who] when it still fits, else as it was sent."""
        tagged = f"@[{sender}] {part}"
        return self._post(tagged if len(tagged.encode()) <= self.porch.budget() else part, sender)

    def _post(self, text: str, sender: str = "", quiet: float = 5.0) -> bool:
        """One post, never resent: from here we can't tell whether a post got out (the relay that carried it is often
        out of our hearing), so a resend would mostly be airtime spent on a message people already have. Someone who
        missed an answer can knock again."""
        return self.porch.say(text, quiet_s=quiet, max_s=30.0)

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

    def help_segments(self) -> list:
        """What Porchlight is, how to knock, how to add a name: all of it, in two messages when the area is long.
        'Porchlight shows' starts it so other porches recognise the answer (OTHER_HELP_RE)."""
        head = f"Porchlight shows busy channels near {self.area}." if self.area else "Porchlight shows busy channels nearby."
        return [head, "Send porchlight or knock to list them. Post #name to add one"]

    def short_hint(self) -> str:
        return str(self.c("short_hint", "Know a channel? Post #name here"))

    def welcome_hint(self) -> str:
        return str(self.c("welcome_hint", "Know another? Post #name here"))

    def results_segments(self, results: list) -> list:
        where = f"Porchlight {self.area}" if self.area else "Porchlight"
        out = []
        for r in results:
            n, res = r.get("name"), r.get("result")
            if res == "named":
                out.append(f"{n} added to {where}, found {r.get('total', 0)} msgs here!")
            elif res == "known":
                out.append(f"{n} is already on {where}")
            elif res == "saved":
                out.append(f"{n} added to {where}")
        return out

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

    def span(self) -> str:
        """The look-back window in words people read at a glance: '3 days', '12h' (plain wording)."""
        h = self.top_hours
        return f"{h // 24} days" if h % 24 == 0 and h >= 48 else f"{h}h"

    def top_text(self) -> str:
        # "#echo 45" = messages in the window; automated channels are marked "(bots)" so nobody expects people there.
        # Every channel goes out: what doesn't fit continues in the next message.
        items = [f"{n} (bots)" if a else f"{n} {c}" for n, c, full, a in self._rows()]
        return f"Busiest, {self.span()}: " + ", ".join(items) if items else ""

    def welcome_text(self) -> str:
        where = f" near {self.area}" if self.area else ""
        items = [f"{n} (bots)" if a else n for n, _, _, a in self._rows()]
        if items:
            return f"Welcome! Active{where}: " + ", ".join(items)
        return f"Welcome! No hashtag channels heard{where} in {self.span()}."

    def newest_text(self, count: int) -> str:
        view, now = self.view_fn() or {}, time.time()
        days = float(self.c("newest_days", 7)) * DAY
        found = [(c["found_at"], c["name"]) for c in view.get("channels", [])
                 if c.get("kind") == "discovered" and c.get("found_at") and c.get("name") != CHANNEL
                 and now - c["found_at"] <= days and now - (c.get("last") or 0) <= days]
        items = [n for _, n in sorted(found, reverse=True)[:count]]
        return f"New in {round(days / DAY)} days: " + ", ".join(items) if items else ""

    def region_segments(self, new: bool = True) -> list:
        r = (self.view_fn() or {}).get("regions") or {}
        if not r:
            return []
        hours = round(r.get("window_days", 3) * 24)
        top = ", ".join(f"{x['name']} {x['count']}" for x in r.get("top", [])[:5])
        other = f" (+{r['unknown']} unknown)" if r.get("unknown") else ""
        segs = [f"Top regions {hours}h: {top or 'none we carry'}{other}"] if top or other else []
        if new:
            fresh = [x["name"] for x in r.get("new_heard", []) if time.time() - (x.get("first") or 0) <= NEW_REGION_DAYS * DAY]
            if fresh:
                segs.append("New regions: " + ", ".join(fresh))
        return segs

    def no_regions_text(self) -> str:
        r = (self.view_fn() or {}).get("regions") or {}
        return f"No region-scoped traffic heard here in {round(r.get('window_days', 3) * 24)}h"

    def pack(self, segs: list, first: int, rest: int | None = None, hint: str = "", sep: str = SEP) -> list:
        """Messages for an answer: everything in segs, as few messages as it takes. first/rest = room in the first
        message (which carries @[who]) and in the ones after. Two or more get 2/3 labels. A segment longer than a
        message continues in the next one, cut between channels; nothing is dropped. hint (the how-to line) goes
        only where there is room left at the end."""
        segs = [x for x in segs if x]
        rest = first if rest is None else rest
        one = sep.join(segs)
        if len(one.encode()) <= first:
            with_hint = one + SEP + hint if hint and one else one
            return [with_hint if len(with_hint.encode()) <= first else one] if one else []
        for label in (LABEL, LABEL + 2):  # "9/9 ", then "10/12 " when it takes ten or more
            b1, b2 = first - label, rest - label
            parts = fill(segs, b1, b2, sep)
            if len(parts) < 10:
                break
        room = b1 if len(parts) == 1 else b2
        if hint and len((parts[-1] + SEP + hint).encode()) <= room:
            parts[-1] += SEP + hint
        n = len(parts)
        return [f"{i}/{n} {p}" for i, p in enumerate(parts, 1)]


def is_list(s: str) -> bool:
    colon = s.find(": ")
    return 0 <= colon < 40


def split_at(s: str, room: int, as_list: bool | None = None) -> tuple[str, str]:
    """The longest start of s that fits room bytes, and the rest. A list ("Busiest, 3 days: #a 4, #b 2") is only cut
    between items, so a name and its count stay together; when not even its first item fits, the heading goes alone.
    Other text (help, replies to names) is cut between words. Never cuts inside a name. ('', s) when nothing fits."""
    if len(s.encode()) <= room:
        return s, ""
    if as_list is None:
        as_list = is_list(s)
    if as_list:  # also the rest of a list continuing in another message, which has no heading any more
        colon = s.find(": ") if is_list(s) else -1
        i = s.rfind(", ")
        while i > colon and len(s[:i].encode()) > room:
            i = s.rfind(", ", 0, i)
        if i > colon:
            return s[:i], s[i + 2:]
        if colon > 0 and len(s[:colon].encode()) <= room:
            return s[:colon], s[colon + 2:]
        return "", s
    i = s.rfind(" ")
    while i > 0 and len(s[:i].encode()) > room:
        i = s.rfind(" ", 0, i)
    return (s[:i], s[i + 1:]) if i > 0 else ("", s)


def fill(segs: list, b1: int, b2: int, sep: str) -> list:
    """Segments into messages of b1 bytes (the first) and b2 (the rest). A segment that doesn't fit what is left of
    a message starts a new one; one longer than a whole message is cut between channels and continues in the next.
    Nothing is dropped and no name is cut."""
    parts, cur, cap = [], "", b1
    for seg in segs:
        s, as_list = seg, is_list(seg)
        while s:
            if cur:
                if len((cur + sep + s).encode()) <= cap:
                    cur, s = cur + sep + s, ""
                    continue
                parts.append(cur)  # doesn't fit the rest of this message: start the next one
                cur, cap = "", b2
            head, rest = split_at(s, cap, as_list)
            if not head:  # one word longer than a whole message (MeshCore names never are): cut it, nothing lost
                cut = cap
                while cut > 1 and len(s[:cut].encode()) > cap:
                    cut -= 1
                head, rest = s[:cut], s[cut:]
            if rest:
                parts.append(head)
                cur, cap, s = "", b2, rest
            else:
                cur, s = head, ""
    return parts + ([cur] if cur else [])
