# Changelog

## 0.11.1

Fixes from the openHop plugin catalogue review.

- **The hourly cap counts every transmission and survives a restart.** Porchlight no longer resends answers, so
  `max_posts_per_hour` covers everything it sends. The last hour's posts are kept in `porch.json`, so restarting
  doesn't reset the count.
- **No resends, no CoreScope.** The resend check depended on hearing a relay, and the relay that carried a post is
  often out of the repeater's hearing, so it mostly resent answers people already had. Removed with it: the
  `reach_check_url`, `reach_observer` and `local_repeaters` settings and the CoreScope name-source type (an existing
  CoreScope source shows as failed, with a note to remove it). Name sources are `.json` catalogs and `.txt` lists.
- **Turn off works while openHop's API fails.** Turn off, Remove and Light the porch are handled on every pass even
  when reading the packet log fails (a refused token, an openHop restart).
- **Private channel keys follow the settings.** A changed key replaces the old one, and a removed private channel goes
  with its key and messages, including from the saved state. A settings file caught mid-write no longer counts as
  empty settings.
- **Remove always finishes.** A companion that isn't there (the porch was never lit, or an earlier Remove deleted it)
  counts as removed, so the page goes on to revoke the API token.
- **The API token is never forgotten while it may still work.** If revoking it fails, the page says so and keeps the
  token and its id so Remove can try again; a token openHop no longer has (404) counts as revoked. If Connect makes a
  token but can't save the settings, it revokes that token.
- **Remove works after the token was revoked outside Porchlight.** When openHop refuses Porchlight's token, the page
  removes the companion with the owner's dashboard sign-in, so Remove still finishes.
- The settings dialog's API token field is gone (0.10.2 removed it from the porch dialog only): a pasted token has
  no id, so Remove couldn't revoke it. **Connect** is the only way in.
- **One shared names catalogue.** Porches get channel names from one place, rebuilt daily by a GitHub job: the community
  channel catalog and the names public CoreScope instances have seen, plus the extended guess list. A porch reads a
  small index daily and downloads only a part that changed, checked against its SHA-256. Names are only ever added.
  CoreScope is read once a day by the job, never by porches. A name in the catalogue is only tried: a channel still
  counts as new on a porch only when that porch first decodes it. New default `name_sources`; an existing install
  keeps its own list until it adds the new source.
- Tests for each of these, including the page's token handling run in node against a fake openHop.

## 0.11.0

- **Updates for GitHub installs.** openHop checks for updates only for plugins installed from its catalogue, so a
  wheel installed with **Install wheel** never heard of a new version. Porchlight now checks this repository's latest
  release once a day and, for those installs only, shows **Update to x.y.z** on its page. One press downloads the
  wheel (from this repository only, a code constant), checks its SHA-256 against the release, and installs and
  restarts it through openHop's plugin API. Catalogue installs are left to openHop.
- **Finds openHop's API by itself.** `api_base` now defaults to empty, meaning: read `http.port` and `http.host`
  from openHop's config, falling back to `127.0.0.1:8000`. A moved port or the Home Assistant add-on needs no
  setting. An `api_base` you set still wins.
- README: install from the catalogue or straight from GitHub, Home Assistant notes, how updates work.

## 0.10.2

- Lighting the porch sets the companion's path hash mode to the repeater's own. New companions start on 1-byte paths,
  so a repeater using 2-byte paths had a companion that didn't match it. To pick up a later change, light the porch again.
- **Remove** says "Removed" and revokes the token only once the companion is really gone. If Porchlight doesn't confirm
  it, or openHop refuses, the page says so and keeps the token so Remove can be tried again.
- The optional token field is gone: **Connect** is the only way in, so Remove can always revoke the token.

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
