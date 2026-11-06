# Changelog

## 0.10.1

- Names dropped from a name list (or a removed source) stop being tried right away, instead of at the next
  settings change.

## 0.10.0 (not released)

- **Extended name list:** about 170,000 likely hashtag channel names (numbers, everyday words, places, and the
  `#townmesh` / `#town-wx` / `#de-town` patterns people use), built by `tools/build_names.py`, attached to every
  release and downloaded weekly by every porch as the default name source "Porchlight extended names".
- Topics people name channels after, beyond places and words: `tools/topics.txt` (radio services like `#gmrs`,
  `#frs`, `#murs`, ham modes and nets, emergency and weather, LoRa hardware, hobbies), with plurals, plus topic
  suffixes on big places (`#ohio-skywarn`, `#indianapolis-gmrs`).
- New name source type `list`: a plain-text file, one name per line, optionally gzipped.
- A guessed name must decode to a real channel message (sender, text, timestamp after 2020), so a big list can't
  produce false channels.
- New names are tried only against stored messages with the same channel hash (seconds instead of hours).

## 0.9.2

- Light the porch tries up to three times, each with a new key and a free port. Until openHop restarts, a deleted
  companion keeps its one-byte key prefix reserved, so a new companion whose random key repeats it is saved but
  never started.

## 0.9.1 (not released)

- Light the porch picks a companion port nothing is listening on, and counts the porch as lit only when openHop
  knows the companion by name. openHop keeps a deleted companion's port busy until it restarts, so after Remove a
  new companion on the same port never started and every post failed with 404.
- A post that fails because openHop no longer knows the companion turns the porch off with a clear reason.

## 0.9.0 (not released)

- **Connect button.** Porchlight creates its own openHop API token (named `porchlight`) with the dashboard sign-in, so
  setup is Connect, then Light the porch. Pasting your own token still works.
- **Remove** now also revokes the token Connect made, after the companion is deleted, so uninstalling leaves nothing
  behind in openHop.
- The plugin page loads nothing from the internet (system fonts), so it works on an off-grid repeater.
- The built-in name list is general words only; local place names come from the name sources.
- The manifest's default settings now match `config.default.json`; tests check the versions and defaults agree.
- A fresh install announces nothing for its first 30 minutes: its first scan names many channels at once, and
  that isn't news (no burst of posts in #porchlight).
- Light the porch works on a repeater with no location set, as long as an area name is typed.
- Logo (`ui/logo.svg`).

## 0.8.1

- Every post fits one 99-byte packet; longer answers go out as numbered parts (`1/2`), spaced apart.
- A resend is the identical packet (same text and timestamp), so nobody sees it twice.
- Optional CoreScope check (`reach_check_url`, `reach_observer`) before resending.
- Help and a knock in the same minute both get an answer.

## 0.8.0

- First release as a stand-alone openHop plugin: Porchlight answers in `#porchlight` through its own companion.
