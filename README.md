<img src="plugin/ui/logo.svg" alt="Porchlight logo" width="96">

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
| `porchlight` (or `porch light`, `#porchlight`, `knock`, `knock knock`) | First time: one welcome message with the active channels. After that: the busiest channels, the newest names found and any region scopes heard. |
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

Porches also learn from each other. Any `#name` in a message Porchlight can read, including another porch's post,
becomes a name it tries. So a channel found by one porch can soon be named by the others. Sharing a name shares nothing
else: a channel only appears here once this repeater hears traffic on it, and its ranking counts only the messages this
repeater heard. A channel busy near another porch won't rank here just because that porch mentioned it.

## Install

You need an openHop Repeater 1.1.4 or newer (the first release with plugins) and its dashboard login.

**From the openHop plugin catalogue** (once listed): **Plugins**, find Porchlight, press **Install**, then **Open UI**.

**Straight from GitHub** (works today, on any openHop install):

1. Download `porchlight_openhop_plugin-<version>-py3-none-any.whl` from the
   [latest release](../../releases/latest).
2. In the openHop dashboard, open **Plugins**, press **Install wheel** and choose the file. The upload goes through
   your browser, so nothing has to be copied onto the repeater.
3. Press **Enable** on Porchlight (a plugin uploaded by hand starts disabled), then **Open UI**.

Both ways end the same: two buttons on the Porchlight page (below), and updates offered on their own.

**Home Assistant:** the openHop Repeater add-on (Main or Dev channel) runs plugins the same way. Install from the
catalogue or with **Install wheel** in the add-on's openHop dashboard (port 8000). Porchlight finds openHop's API and
keeps its data in the add-on's storage, so it survives add-on updates and restarts.

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

- **Installed from the catalogue:** openHop's Plugins page offers each update once the catalogue approves it.
- **Installed from GitHub:** openHop can't check those for updates, so Porchlight does. It looks at this project's
  [releases](../../releases) once a day, and when there is a newer one the Porchlight page shows **Update to x.y.z**.
  One press downloads the wheel from this repository only, checks its SHA-256 against the release, and installs it
  through openHop's own plugin install. Nothing is installed without that press.

Settings, channel history and the lit porch are kept either way.

## Uninstall

1. Open Porchlight, press **Light the porch**, then **Remove**. This deletes Porchlight's companion from openHop and
   revokes the `porchlight` API token that Connect made.
2. On the Plugins page, uninstall Porchlight. Choose whether to keep its data folder.

openHop finishes deleting a companion only when it restarts: until then the old companion keeps its port. Lighting the
porch again before that works (Porchlight picks another port and key), and restarting openHop at some point tidies up.

## Safety: what it sends, and to where

- **Radio.** It only posts in `#porchlight` and never sends DMs. The channel is fixed in the code, not a setting.
  Before every post, Porchlight reads the key in its companion's channel slot. If anything changed it, Porchlight goes
  dark until its owner lights it again. It transmits at most `max_posts_per_hour` (30) times an hour, counting
  every transmission, and the count survives a restart. It waits for quiet air before each post and never resends
  on its own: someone who missed an answer can knock again.
- **openHop changes.** Light the porch adds one companion identity (`porchlight`, bound to `127.0.0.1` on a free port
  between 5060 and 5099) to openHop's configuration. Remove deletes it. Nothing else in openHop is changed.
- **Credentials.** Connect creates one openHop API token named `porchlight`. openHop API
  tokens are not scoped: they can do anything the dashboard can, so Porchlight only uses it for the calls listed below
  and only sends it to the repeater's own API (`api_base`, normally `http://127.0.0.1:8000`). It is stored in the plugin's
  settings (`config.json` in its data folder). **Remove** revokes it.
- **openHop API calls.** Read: packet log (`filtered_packets`, `recent_packets`, `packet_by_hash`), `stats`,
  `identities`, `transport_keys`, the plugin's own settings and runtime. Write: create and delete its own companion
  identity, set its name, send in `#porchlight`. The page itself also creates and revokes the `porchlight` token, and
  for an update you pressed, installs and restarts Porchlight through openHop's plugin API.
- **Network.**
  - One [OpenStreetMap Nominatim](https://nominatim.org) reverse lookup when you light the porch. The location is
    rounded to about 1 km first.
  - Downloads of the channel-name lists you configure. By default only Porchlight's shared names catalogue (see
    Channel names): a small index once a day from this project's GitHub Pages, and a part of the catalogue only
    when it changed (usually a few KB; the 0.6 MB guess list only when it is rebuilt).
  - Once a day, this repository's latest-release information from GitHub's API (a version number and file
    checksums), and the new wheel only when you press **Update** (installs from GitHub only; see Update).
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
| `name_sources` | Porchlight names | Where channel names come from. Use **Add source** for another names index, a `.json` catalog or a `.txt`/`.txt.gz` list |
| `area` | looked up | The area named in answers |
| `auto_channels` | `#wardriving` | Automated channels: listed last and marked "auto", never announced |
| `ignore_names` | none | Other bots whose posts shouldn't make a channel look busy |
| `api_base` | found by itself | Where openHop's API is. Empty = read `http.port` (and `http.host`) from openHop's config; set it only if that fails |
| `max_posts_per_hour` | 30 | Hard cap on transmissions, kept across restarts |
| `top_count`, `top_hours` | 4, 72 | How many of the busiest channels to list, over how many hours |

## Channel names

A hashtag channel's key comes from its name, so Porchlight can read one as soon as it tries the right name. Private
channels have random keys and are never affected. Besides names people send in and `#tags` seen in chat, every porch
uses one shared names catalogue, rebuilt daily by [`tools/build_index.py`](tools/build_index.py) and published at
`https://mutiny19.github.io/porchlight-openhop-plugin/names/index.json`:

- **Real names:** the community [MeshCore channel catalog](https://github.com/marcelverdult/meshcore-channels) and the
  channel names that public [CoreScope](tools/corescopes.json) instances have seen. The daily job reads each of them once
  a day for every porch together; porches never contact them.
- **Guesses:** built by [`tools/build_names.py`](tools/build_names.py): short numbers, everyday English words and their
  plurals, topics from [`tools/topics.txt`](tools/topics.txt) (`#gmrs`, `#skywarn`, `#js8call`, `#preppers`, ...), place
  names, and the patterns the catalog shows people use (`#townmesh`, `#town-wx`, `#de-town`, ...). About 170,000 names,
  most likely first, capped at 2 MB. Rebuilt monthly.

A porch reads the small index once a day and downloads a part only when its SHA-256 changed, keeping it only if the
bytes match. Names are only ever added, on the porch and in the catalogue: a name that drops out of a source is still
tried. A name in the catalogue is only something to try: a channel shows up on a porch, and counts as new there, only
when that porch decodes a real message on it (a sender, the text, and a timestamp after 2020).

## Requirements

- openHop Repeater **1.1.4 or newer**. Every API Porchlight uses is in 1.1.4; it runs every day on 1.1.5 (development
  build) on a Raspberry Pi 4 with Python 3.13.
- Python 3.9 or newer on the repeater. openHop installs Porchlight's one dependency, `pycryptodome`, into the plugin's
  own environment (the repeater needs internet access during install for that).
- The repeater's location set in openHop, for the area name, or type the area by hand.
- Nothing to set for openHop's API: Porchlight reads `http.port` and `http.host` from openHop's
  `/etc/openhop_repeater/config.yaml` (also there in the Home Assistant add-on), falling back to `127.0.0.1:8000`. Only
  if openHop's config lives somewhere else and its port isn't 8000, set `api_base` (Plugins, Porchlight, **Config**).
- Nothing else: no CoreScope, MQTT, bot or other plugin is needed. Extra name lists are optional.

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

## If you fork this

Please keep the companion named **Porchlight** and the channel **`#porchlight`**. Porches only work together because
they all use that name and that channel: it's how they recognise each other, stay quiet after another porch answers,
and learn channel names from each other. A fork that renames either one drops out of that shared network.

A fork should also keep Porchlight's purpose. It should never post in any other channel, and it should stick to
discovering channels and sharing the ones with real local activity. Everyone running a porch is trusting it to do only that.

## Credits and license

A community project, started by [HOOP](https://mutiny19.github.io/hoop/). No one person or group owns it. Channel names come from the community
[MeshCore channel catalog](https://github.com/marcelverdult/meshcore-channels) (CC0), public CoreScope instances, and
from people on the mesh.
Area names © OpenStreetMap contributors. The extended name list uses place names from
[GeoNames](https://www.geonames.org/) (CC BY 4.0) and words from [SCOWL](https://github.com/en-wl/wordlist).

Porchlight is free software under the [GNU General Public License v3.0](LICENSE).
