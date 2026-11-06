# Porchlight

**An openHop plugin that leaves the porch light on for anyone new to the mesh.**

Someone new to MeshCore joins `#porchlight` and sends `porchlight`. The nearest repeater running Porchlight answers
with the channels people are actually using nearby:

```
Porchlight: @[Visitor] 1/2 Welcome! Active near Springfield, IL: #test, #wx, #echo
Porchlight: @[Visitor] 2/2 New in 7 days: #nky | Know a channel? Post #name here
```

Every message fits a 99-byte packet, whatever the asker's name. On a busy repeater, longer packets are the first to be
lost, so a longer answer goes out as numbered parts spaced a few seconds apart instead of as one big message.

Porchlight also shows the repeater's owner every channel it hears, with the last 100 messages of each channel it can
read.

## What people on the mesh can do in #porchlight

| Send | Porchlight answers |
|---|---|
| `porchlight` (or `porch light`, `#porchlight`) | First time: one welcome message with the active channels. After that: the busiest channels, the newest names found and any region scopes heard. |
| `help` | What Porchlight is, how to ask, and how to add a channel name. |
| `#name` (1 to 3 names) | Adds hashtag channel names. If this repeater already heard that channel, it says so. |
| `porchlight regions` | Region scopes heard in the last 72 hours. |
| `porchlight 2/3` (or `2 of 3`) | Resends a part of a longer answer that got lost. |

Porchlight never answers someone who hasn't asked, and it only ever posts in `#porchlight`.

When a hashtag channel is new here (heard for the first time, or back after a week of silence), Porchlight posts its
name in `#porchlight`, like `#name (Springfield, IL)`.

## Several porches

Every install is called **Porchlight** and says its area in the text. When several hear the same request, the one
nearest the asker answers first; the others hear that answer and stay quiet. Announcements wait a random moment and are
skipped if another porch already posted the same one. Anyone answered by any porch counts as welcomed by all of them.

## Install

You need an openHop Repeater 1.1.4 or newer (the first release with plugins) and its dashboard login.

**From a release file** (until Porchlight is in the openHop plugin catalogue):

1. Download `porchlight_openhop_plugin-<version>-py3-none-any.whl` from this repository's [Releases](../../releases).
2. In the openHop dashboard, open **Plugins**, press **Install wheel** and choose the file.
3. Press **Enable** on Porchlight (a plugin uploaded by hand starts disabled), then **Open UI**.

**From the openHop plugin catalogue** (once listed): **Plugins**, find Porchlight, press **Install**, then **Open UI**.

## Set up: two buttons

1. **Connect.** The first time you open Porchlight it asks to connect. This creates an openHop API token named
   `porchlight` with your dashboard sign-in, so Porchlight can read the repeater's packet log. Channels start to
   show up within a few seconds.
2. **Light the porch.** Check the area name Porchlight looked up from the repeater's location (type a different one
   to override it) and press **Light the porch**. Porchlight creates a companion named "Porchlight" on `127.0.0.1`,
   puts `#porchlight` in its channel slot and starts answering.

That's all.

**Turn off** stops Porchlight from talking; it keeps reading channels.

## Update

From the catalogue, the Plugins page offers the update. With a release file: **Disable** Porchlight, **Install wheel**
with the newer file, then **Enable** it again. Settings, channel history and the lit porch are kept.

## Uninstall

1. Open Porchlight, press **Light the porch**, then **Remove**. This deletes Porchlight's companion from openHop and
   revokes the `porchlight` API token that Connect made.
2. On the Plugins page, uninstall Porchlight. Choose whether to keep its data folder.

openHop finishes deleting a companion only when it restarts: until then the old companion keeps its port. Lighting the
porch again before that works (Porchlight picks another port and key), and restarting openHop at some point tidies up.

## Safety: what it sends, and to where

- **Radio.** It only posts in `#porchlight` and never sends DMs. The channel is fixed in the code, not a setting.
  Before every post, Porchlight reads the key in its companion's channel slot. If anything changed it, Porchlight goes
  dark until its owner lights it again. It posts at most 30 messages an hour, waits for quiet air before each one,
  and resends an answer once only if no repeater was heard passing it on. A resend is the identical packet (same text,
  same timestamp), so anyone who already got it never sees it twice. Optionally, set `reach_check_url` (a CoreScope that
  hears your area) and `reach_observer` (this repeater's observer name there): Porchlight then skips the resend when
  other observers heard the answer, and posts a fresh copy when nobody did, which gets past a relay that dropped the first.
- **openHop changes.** Light the porch adds one companion identity (`porchlight`, bound to `127.0.0.1` on a free port
  between 5060 and 5099) to openHop's configuration. Remove deletes it. Nothing else in openHop is changed.
- **Credentials.** Connect creates one openHop API token named `porchlight`. openHop API
  tokens are not scoped: they can do anything the dashboard can, so Porchlight only uses it for the calls listed below
  and only sends it to the repeater's own API (`api_base`, `http://127.0.0.1:8000`). It is stored in the plugin's
  settings (`config.json` in its data folder). **Remove** revokes it.
- **openHop API calls.** Read: packet log (`filtered_packets`, `recent_packets`, `packet_by_hash`), `stats`,
  `identities`, `transport_keys`, the plugin's own settings and runtime. Write: create and delete its own companion
  identity, set its name, send in `#porchlight`. The page itself also creates and revokes the `porchlight` token.
- **Network.**
  - One [OpenStreetMap Nominatim](https://nominatim.org) reverse lookup when you light the porch. The location is
    rounded to about 1 km first.
  - Downloads of the channel-name lists you configure. By default: the community
    [MeshCore channel catalog](https://github.com/marcelverdult/meshcore-channels) daily, and Porchlight's extended
    name list (about 170,000 names, 0.6 MB) weekly from this repository's latest release.
  - Only if you set `reach_check_url`: one look-up of each of Porchlight's own posts on that CoreScope.
  - Nothing else leaves the repeater. The plugin page loads nothing from the internet, so it works off-grid.
- **Data kept.** Up to 100 messages per readable channel, the names people sent in, which visitors were welcomed (by
  their mesh name), and region-scope statistics. All of it stays in the plugin's data folder.

## Settings

Everything is under **Channels and keys** and **Light the porch** on the plugin page, stored in `config.json`:

| Setting | Default | What it does |
|---|---|---|
| `hashtags` | `#test`, `#wardriving` | Channels to always read (`#porchlight` is always read) |
| `private` | none | Private channels you're a member of (name and 16-byte key), shown only to you |
| `discovery` | on | Try names from lists and from `#tags` seen in chat |
| `name_sources` | channel catalog, extended names | Lists of channel names. Use **Add source** for a CoreScope address, a `.json` catalog or a `.txt`/`.txt.gz` list |
| `area` | looked up | The area named in answers |
| `auto_channels` | `#wardriving` | Automated channels: listed last and marked "auto", never announced |
| `local_repeaters` | none | Your own next-door repeaters (hash prefixes); their relays don't count as "it got out" |
| `ignore_names` | none | Other bots whose posts shouldn't make a channel look busy |
| `api_base` | `http://127.0.0.1:8000` | Where openHop's API is (change only if you moved openHop's port) |
| `max_posts_per_hour` | 30 | Hard cap on posts |
| `top_count`, `top_hours` | 4, 72 | How many of the busiest channels to list, over how many hours |
| `reach_check_url`, `reach_observer` | none | Optional CoreScope check before resending (see Safety) |

## Extended name list

A hashtag channel's key comes from its name, so Porchlight can read one as soon as it tries the right name. Private
channels have random keys and are never affected. Besides the community catalog and names people send in, every porch
downloads an extended list built by [`tools/build_names.py`](tools/build_names.py): short numbers, everyday English
words and their plurals, topics from [`tools/topics.txt`](tools/topics.txt) (`#gmrs`, `#skywarn`, `#js8call`, `#preppers`,
...), place names, and the patterns the catalog shows people use (`#townmesh`, `#town-wx`, `#de-town`, ...). About
170,000 names, most likely first, capped at 2 MB. A guessed name only counts when it decodes to a real channel message
(a sender, the text, and a timestamp after 2020). The Release workflow rebuilds the list for every release.

## Requirements

- openHop Repeater **1.1.4 or newer**. Every API Porchlight uses is in 1.1.4; it runs every day on 1.1.5 (development
  build) on a Raspberry Pi 4 with Python 3.13.
- Python 3.9 or newer on the repeater. openHop installs Porchlight's one dependency, `pycryptodome`, into the plugin's
  own environment (the repeater needs internet access during install for that).
- The repeater's location set in openHop, for the area name, or type the area by hand.
- openHop's API at `http://127.0.0.1:8000`, the default. If you changed openHop's `http.port`, or bound `http.host`
  to one address only, set Porchlight's `api_base` to match (Plugins, Porchlight, **Config**).
- Nothing else: no CoreScope, MQTT, bot or other plugin is needed. CoreScope and extra name lists are optional extras.

## Problems and questions

Open an [issue](../../issues). Include the openHop version and the last lines of the plugin log (Plugins, Porchlight,
**Logs**). Never paste your API token.

## Development

```bash
python -m venv .venv && .venv/bin/pip install pycryptodome pytest build
.venv/bin/python -m pytest -q          # tests: no radio, network or openHop needed
.venv/bin/python -m build --wheel      # dist/porchlight_openhop_plugin-<version>-py3-none-any.whl
```

To release, put the same version in `pyproject.toml`, `plugin/openhop-plugin.json` and `porchlight/__init__.py`, then
push a tag like `v0.8.0`. The Release workflow builds the wheel from that commit and publishes it with its SHA-256.

## Credits and license

Made by [HOOP](https://mutiny19.github.io/hoop/). Channel names come from the community
[MeshCore channel catalog](https://github.com/marcelverdult/meshcore-channels) (CC0) and from people on the mesh.
Area names © OpenStreetMap contributors. The extended name list uses place names from
[GeoNames](https://www.geonames.org/) (CC BY 4.0) and words from [SCOWL](https://github.com/en-wl/wordlist).

Porchlight is free software under the [GNU General Public License v3.0](LICENSE).
