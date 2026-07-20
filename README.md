# TimeScribe Desktop

A Windows system-tray app for MSP technicians that **captures what you actually
worked on all day and turns it into reviewed, ticket-attached time entries in
HaloPSA** — plus an MCP server that makes your activity record conversational
from Claude Desktop, Cowork, or Claude Code.

Built for the technician who ends the day thinking *"where did those eight
hours go?"* — and doesn't want to reconstruct it from memory.

## What it does

- **Passive activity capture** — Microsoft Edge browsing history across all
  profiles (profile = client attribution), application window focus via a
  bundled [ActivityWatch](https://activitywatch.net), and AFK/asleep/locked
  detection so idle time is never billed.
- **AI activity digest** — every 2 hours during work hours, the day so far is
  summarized into timestamped, human-readable entries ("Configured integration
  runbooks for Contoso on HaloPSA", not "browsed 47 pages").
- **Draft time entries with full curation** — import digest entries as drafts,
  assign tickets from a searchable picker of your open Halo tickets (or leave
  blank for Quick Time), edit times/notes, split entries, approve, and post.
  An optional AI pass can suggest ticket matches.
- **End-of-day nudge** — at 17:15 a Windows toast tells you your drafts are
  ready. Review and post the whole day in two minutes.
- **MCP server** (read-only, deliberately scoped) — exposes browser history,
  window activity, inactivity periods, and stored digests to Claude. No
  ticket or time-entry powers; those stay in the app behind human review.

## How it fits together

```
Edge history ─┐
ActivityWatch ┼─→ digest (LLM) ─→ drafts ─→ [you review] ─→ HaloPSA
AFK detection ┘        │
                       └─→ MCP server ─→ Claude Desktop / Cowork / Claude Code
```

## Install

### Option A — from source (recommended)

Release binaries are not yet code-signed, so Windows SmartScreen will fight
you on downloads. Building from source sidesteps that entirely and takes one
command:

```powershell
git clone https://github.com/Jen-Butler/timescribe.git
cd timescribe
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` checks for Python 3.10+ (installs it via winget if missing),
installs dependencies, downloads the ActivityWatch portable bundle, creates
Start Menu + run-at-login shortcuts, and launches the app. Flags:
`-SkipAW` (ActivityWatch already installed), `-NoStartup` (no login
shortcut), `-BuildExe` (also freeze a standalone exe), `-Uninstall`
(clean removal, optionally including all data and credentials).

### Option B — installer

Download `TimeScribe-Setup-<version>.exe` from the
[Releases page](https://github.com/Jen-Butler/timescribe/releases) and run
it — installs per-user (no admin), optional start-at-login, bundles
ActivityWatch. Until releases are code-signed, expect a SmartScreen warning:
⋯ → Keep → Show more → Keep anyway.

### First-run setup (either option)

1. First launch opens the dashboard. Fill in the **Setup** card:
   - **HaloPSA URL** and **OAuth Client ID** (see *Halo OAuth app* below)
   - **AI provider**: Anthropic, OpenAI, or **MCP only** (no API key —
     in-app AI disabled, activity data still available to Claude via MCP)
   - API key for the chosen provider (stored in Windows Credential Manager,
     never on disk)
2. Click **Connect to Halo** — sign in with your normal Halo credentials in
   the browser window that opens. The app acts as *you* from then on.
3. Optional: drag `TimeScribeActivity-<version>.mcpb` into
   Claude Desktop → Settings → Extensions to chat with your activity data.

### Registering the Halo OAuth app (once per Halo instance, admin)

Configuration → Integrations → Applications → **New**:
- Grant type: **Authorization Code** (PKCE — no client secret needed)
- Redirect URI: `http://localhost:8765/oauth/callback` (exact match)
- Scopes: `all` (or read/edit tickets + actions at minimum)

Share the generated **Client ID** with your technicians; there is no secret
to protect because the app is a public PKCE client.

## Daily workflow

1. Work normally. The tray app digests every 2h; ActivityWatch runs invisibly.
2. 17:15 toast: *"N draft time entries ready."* Click the tray icon.
3. In the dashboard: **import from digest** (instant, no AI cost) or
   **AI ticket match** (LLM suggests ticket assignments).
4. Curate: edit times/notes, pick tickets from the dropdown, **split** long
   entries, approve the keepers, reject the noise.
5. **post approved** — ticket-attached entries land on their tickets;
   blank-ticket entries post as `Quick Time - <you> - <date>`.
6. The summary strip tracks captured / drafted / approved / posted / unbilled
   hours; the posted log keeps 14 days of history with links into Halo.

## Uninstall

**Source install:** `powershell -File setup.ps1 -Uninstall` — removes
shortcuts, stops the app, uninstalls the package, and offers to purge all
data and credentials. Then delete the cloned folder.

**Installer:** Windows Settings → Apps → TimeScribe → Uninstall (or the
uninstaller in the
install folder). The uninstaller stops the app and any bundled ActivityWatch
processes, removes all installed files and shortcuts, and then **asks whether
to also delete your data** — settings, activity digests, draft time entries,
logs, and the stored credentials in Windows Credential Manager. Answer *Yes*
for a complete removal with no traces; answer *No* to keep your data for a
future reinstall. Time entries already posted to HaloPSA are never touched.

## Troubleshooting

**"spawn EPERM" when installing the Claude Desktop extension.** The .mcpb
bundles an unsigned `timescribe-mcp.exe`; Windows sometimes refuses to run
it. To confirm and fix, on the affected machine:

1. Check whether Defender removed or blocked it: Windows Security →
   Virus & threat protection → Protection history. If quarantined, choose
   Restore and add an Allow action, then disable/re-enable the extension in
   Claude Desktop → Settings → Extensions.
2. If you have **Smart App Control** on (Windows Security → App & browser
   control), it blocks all unsigned executables and cannot allowlist
   individual files. Until the binary is code-signed, the extension can't
   run on such machines.
3. Otherwise try launching the binary directly in a terminal:
   `& "$env:APPDATA\Claude\Claude Extensions\<timescribe folder>\server\timescribe-mcp.exe"`
   — the error shown there tells you what's blocking it.

**Python traceback popup mentioning `FileExistsError` and `aw-qt` right
after install.** Harmless one-time race between two ActivityWatch launches
(fixed in source after v0.1.1). Everything still works; dismiss the dialog.

## Development

Requires Python 3.10+ on Windows.

```bash
python -m venv .venv
source .venv/Scripts/activate        # Git Bash; or .venv\Scripts\activate in cmd
python -m pip install -e .
python -m timescribe app        # tray + dashboard
python -m timescribe.mcp_server # stdio MCP server (dev)
```

Dev CLI helpers (`python -m timescribe ...`): `connect` runs the
Halo OAuth flow, `test-list` smoke-tests ticket fetching, `app` launches the
desktop app.

### Project layout

```
timescribe/
  app.py            tray icon, dashboard launcher, service supervision
  server.py         FastAPI backend for the local dashboard (127.0.0.1 only)
  ui/index.html     single-file dashboard UI
  appconfig.py      config (%APPDATA%) + secrets (Credential Manager)
  history.py        Edge multi-profile SQLite history reader
  activitywatch.py  ActivityWatch REST client (window focus, AFK)
  aw_manager.py     launches/supervises bundled ActivityWatch
  digest.py         signal merge + LLM activity digest
  drafts.py         draft time-entry persistence + posted log + summaries
  inference.py      optional LLM ticket-matching pass
  llm.py            provider-agnostic LLM shim (Anthropic / OpenAI)
  scheduler.py      in-app scheduler: 2h digests, EOD drafts + toast
  mcp_server.py     read-only MCP server (FastMCP, stdio)
  psa/
    adapter.py      abstract PSA interface (Ticket, TimeEntry, CalendarEvent)
    halo.py         HaloPSA adapter (OAuth PKCE, appointments)
  oauth/pkce.py     PKCE flow + localhost callback server
```

### Building release artifacts

```bash
python fetch_aw.py                     # one-time: download ActivityWatch portable (~100 MB)
python -m PyInstaller pad.spec --noconfirm     # desktop app -> dist/TimeScribe/
"C:/Program Files/Inno Setup 7/ISCC.exe" installer.iss   # -> installer_out/*.exe
python build_mcpb.py                   # MCP bundle -> TimeScribeActivity-*.mcpb
```

Note: antivirus may quarantine freshly built unsigned executables — add a
folder exclusion while developing. Code-sign before distributing broadly.

## Security & privacy notes

- Everything binds to `127.0.0.1`; nothing listens on the network.
- Halo auth is OAuth 2.0 Authorization Code + PKCE — the distributed binary
  contains no secret, and users authenticate with their own Halo credentials.
- API keys and refresh tokens live in Windows Credential Manager via
  `keyring`, never in config files.
- Activity data (URLs, window titles, snippets) is sent to your chosen LLM
  provider when digests run. In **MCP-only** mode, nothing leaves the machine
  unless you ask Claude about it.
- The MCP server is intentionally read-only: it cannot create time entries,
  touch tickets, or modify anything.

## Roadmap

- ConnectWise Manage adapter (API-key model; `psa/adapter.py` is ready for it)
- Code-signing for AV-friendly distribution
- Remote ActivityWatch hosts (multi-machine capture) — config key exists
- Week view and richer reporting in the dashboard

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [ActivityWatch](https://activitywatch.net) (MPL-2.0) — bundled unmodified
  for window/AFK tracking.
