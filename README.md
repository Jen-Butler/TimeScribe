# TimeScribe Desktop

A Windows system-tray app for MSP technicians that **captures what you actually
worked on all day and turns it into reviewed, ticket-attached time entries in
HaloPSA** — reviewable from the desktop dashboard or your phone, plus an MCP
server that makes your activity record conversational from Claude Desktop,
Cowork, or Claude Code.

Built for the technician who ends the day thinking *"where did those eight
hours go?"* — and doesn't want to reconstruct it from memory.

## What it does

- **Passive activity capture** — browser history across all profiles of your
  installed browsers (profile = client attribution), application window focus
  via a bundled [ActivityWatch](https://activitywatch.net), and
  AFK/asleep/locked detection so idle time is never billed. Optional local
  screenshots give you a visual record to check against.
- **AI activity log and drafts** — on a schedule during work hours, the day so
  far is summarized into timestamped entries ("Configured integration runbooks
  for Contoso on HaloPSA", not "browsed 47 pages") and turned into draft time
  entries matched to your open Halo tickets. One entry is one task for one
  client; a new entry starts whenever the context switches.
- **Full curation before anything posts** — edit times and notes, pick a ticket
  from a searchable list of your own tickets (assigned or additional agent),
  split, merge, ignore, regenerate a single note with **Summarize**, and see
  the captured activity behind any entry. Overlaps with the Halo timesheet are
  flagged live.
- **Post, and take it back** — approved entries post to their tickets (or as
  Quick Time). **Unpost** removes an entry from Halo and returns it to drafts.
- **Phone access** — review, edit, and post from your phone's browser on the
  same Wi-Fi, after a one-time pairing.
- **End-of-day nudge** — at 17:15 a Windows toast tells you your drafts are
  ready.
- **MCP server** (read-only, deliberately scoped) — exposes browser history,
  window activity, inactivity periods, and stored digests to Claude. No
  ticket or time-entry powers; those stay in the app behind human review.

## How it fits together

```
Browser history ─┐
ActivityWatch   ─┼─→ activity log (LLM) ─→ drafts ─→ [you review] ─→ HaloPSA
AFK detection   ─┤          │                           ▲
Screenshots     ─┘ (local)  │                desktop dashboard / phone
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
⋯ → Keep → Show more → Keep anyway. Installer builds can lag behind `main`.

### First-run setup (either option)

1. First launch opens the dashboard. In **Settings › General**:
   - **HaloPSA URL** and **Client ID**, and a **sign-in method** (see
     *Connecting to HaloPSA* below)
   - **AI provider**: Anthropic, OpenAI, a shared org key stored in Halo, or
     **MCP only** (no API key — in-app AI disabled, activity data still
     available to Claude via MCP)
   - API key for the chosen provider (stored in Windows Credential Manager,
     never on disk)
2. Click **Connect to Halo**.
3. Optional: drag `TimeScribeActivity-<version>.mcpb` into
   Claude Desktop → Settings → Extensions to chat with your activity data
   (config snippets are under **Settings › Claude / MCP**).

## Connecting to HaloPSA

Register an API application once per Halo instance (admin):
Configuration → Integrations → HaloPSA API → Applications → **New**.

**Browser sign-in (Authorization Code + PKCE) — the default.**
- Authentication method: **Authorization Code**
- Redirect URI: `http://localhost:8765/oauth/callback` (exact match)
- Permissions: `all` (or read/edit tickets + actions at minimum)

Each technician signs in with their own Halo account and the app acts as them.
There is no secret to protect.

**Client ID + secret (Client Credentials) — use when browser sign-in is
blocked.** Some Halo tenants sit behind a firewall that rejects the
`localhost` redirect with a 403. In that case:
- Authentication method: **Client ID and Secret (Services)**
- **Agent to log in as: the technician.** This matters: Halo attributes
  everything the application posts to this agent and ignores the agent sent
  with each entry. If it is left unset, time posts under the application's
  name and lands on nobody's timesheet.
- In TimeScribe, choose this sign-in method and enter the secret and your
  agent ID. The secret is stored in Windows Credential Manager.

TimeScribe checks the attribution at connect time and **refuses to post**
while Halo would credit the time to the application, with the Halo pill
explaining what to change.

## Daily workflow

1. Work normally. The tray app builds the activity log and adds drafts through
   the day; it never changes drafts you already have.
2. 17:15 toast: *"N draft time entries ready."* Open the dashboard or the phone
   page.
3. If needed, rebuild a day yourself:
   - **Run digest** — builds drafts from the existing activity log (replaces
     unposted drafts; posted entries are kept).
   - **Purge & run digest** — re-reads raw activity, rebuilds the log, then
     the drafts.
   - **Fill timesheet gaps** — drafts only the windows Halo has no time for.
4. Curate each entry:
   - Edits autosave. Editing an approved entry drops it back to draft.
   - **Find** a ticket (recently used tickets are suggested first) or leave it
     blank for Quick Time.
   - **Split** (the phone asks for the time to split at), **Merge**,
     **Ignore** (ignored time is not drafted again), **Summarize** (rewrites
     the note from that entry's own activity).
   - The activity peek shows app focus, browser visits by profile, away time,
     and screenshots for the entry's window.
   - Warnings appear for overlaps with the Halo timesheet or other entries.
5. **Approve**, then **Post approved**. Ticketed entries land on their
   tickets at the contract's default charge rate; blank-ticket entries post as
   `Quick Time - <you> - <date>`.
6. Posted the wrong thing? **Unpost** deletes it from Halo and puts it back in
   drafts so you can fix it and post again.
7. The Halo timesheet panel shows what is already logged; click a row to read
   its note. The History tab keeps a posted log and a charge-rate audit.

### How ticket matching behaves

The matcher is deliberately conservative: a wrong ticket is worse than none.

- A ticket is assigned only when the activity concerns that ticket's subject.
  The client's name alone is not enough.
- The evidence has to be inside the entry's own time window. A ticket is not
  carried over from the previous stretch of the day.
- Anything weaker is left unassigned (Quick Time) for you to assign.
- The reason for each match is shown on the entry.

## Phone access

Review and post from your phone's browser while on the same network as the PC.
Nothing is stored on the phone except a pairing key.

1. **Settings › Mobile** → tick *Enable mobile access* → **Save**.
2. Allow the ports through Windows Firewall (the tab has a button; it adds a
   private-network rule). Default ports: **8771** (HTTP) and **8772** (HTTPS).
3. **Pair a phone** → scan the QR code. Codes are single-use and expire in 10
   minutes. The phone is remembered until you revoke it from the device list.
4. Optional, for a home-screen app: the phone is offered TimeScribe's local
   certificate to install once, after which Chrome's **Install app** works
   over HTTPS. Without it, the plain-HTTP page works as a browser tab.

The phone page has the entries list with the same editing tools as the
desktop, the day's activity log, and the Halo timesheet. Settings, API keys,
logs, updates, and screenshot deletion are not reachable from the phone.

Phone access is LAN-only by design. Do not port-forward it to the internet.

## Screenshots (optional)

**Settings › Screenshots** turns on a local recorder: a capture when you
switch windows and at a set interval while you are active (not while idle or
locked). Images stay on the PC, are never uploaded or sent to Halo or the AI,
and are deleted after the retention period (default 14 days). You can exclude
apps, pause, or purge at any time.

Storage depends on capture size and interval. As a guide, full resolution
across two monitors every minute at high quality is roughly 0.5 GB per working
day; 1280 px wide is around 20 MB per day.

## Settings worth knowing

- **Ignore brief app focus** — one threshold used by the activity view, the
  digest, and matching.
- **Known systems** — tells the AI what a website is (your Halo URL is known
  automatically; add client systems as `domain = label`).
- **Home browser profile** — your own company's profile; work there that names
  a client is attributed to that client.
- **ActivityWatch categories** — optionally tag activity with your own AW
  categorization rules.
- **Time display** — 12-hour or 24-hour.
- **AI credits** — providers do not expose a balance to an API key, so enter
  the balance from the provider console and TimeScribe estimates what is left
  from its own usage, with a low-balance warning.
- **Prompts** — the digest and matching instructions are editable; the output
  format is fixed.
- **Appearance** — fonts, colour presets, light/dark.

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

If you enabled phone access, also remove the firewall rule for ports
8771–8772 and the TimeScribe certificate from the phone.

## Troubleshooting

**Halo sign-in shows a 403 page before the login form.** The tenant's firewall
is rejecting the `localhost` redirect. Use the Client ID + secret sign-in
method (see *Connecting to HaloPSA*).

**Posted time shows under the API application's name, or the Halo pill says
posting is blocked.** Set **Agent to log in as** on the Halo API application
to the technician, then reconnect.

**The phone can't reach the PC.** Check that both are on the same network,
that Settings › Mobile shows *listening*, and that the firewall line says the
rule is present for both ports.

**A day's drafts look empty when they shouldn't be.** Each day's drafts are
saved with a backup copy and recovered automatically on load. Restart the app;
if they are still missing, `POST /api/drafts/restore?day=YYYY-MM-DD` on the
local dashboard port restores the last saved copy.

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

Failed requests are written to the app log (**Settings › System**) as
`[api] METHOD path -> status`.

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

Branches: work lands on `dev`; `main` is fast-forwarded to it when a build is
ready to install from.

### Project layout

```
timescribe/
  app.py            tray icon, dashboard launcher, service supervision
  server.py         FastAPI backend: dashboard API, phone gate, pairing
  ui/index.html     single-file desktop dashboard
  ui/mobile.html    single-file phone page
  appconfig.py      config + secrets (Credential Manager)
  history.py        multi-browser, multi-profile history reader
  activitywatch.py  ActivityWatch REST client (window focus, AFK)
  aw_manager.py     launches/supervises bundled ActivityWatch
  digest.py         signal merge + LLM activity log
  inference.py      drafts: ticket matching, gap fill, single-entry summary,
                    match backstops
  prompts.py        editable prompt bodies + fixed output contracts
  systems.py        known-systems legend, browser-profile roles, AW categories
  drafts.py         draft persistence (atomic, backed up), ignore list,
                    overlap/duplicate filters, summaries
  llm.py            provider-agnostic LLM shim (Anthropic / OpenAI) + metering
  aicost.py         usage ledger and estimated credit balance
  screenshots.py    local screenshot recorder + retention
  mobile.py         LAN listeners, device pairing, local CA for HTTPS
  scheduler.py      periodic digests, end-of-day drafts + toast, cache warming
  mcp_server.py     read-only MCP server (FastMCP, stdio)
  psa/
    adapter.py      abstract PSA interface (Ticket, TimeEntry, CalendarEvent)
    halo.py         HaloPSA adapter (PKCE / client credentials, time entries,
                    timesheet, unpost)
  oauth/pkce.py     PKCE flow + localhost callback, client-credentials grant
  _vendor/segno/    bundled QR-code library (BSD-3-Clause)
tools/
  find_test_actions.py   lists time entries attributed to the API application
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

- The dashboard binds to `127.0.0.1` only. Nothing listens on the network
  unless you turn on phone access.
- With phone access on, two extra listeners accept connections from private
  network addresses only. A request must carry a paired device's key and may
  only reach the review endpoints; settings, secrets, logs, updates, and
  screenshot deletion are refused. Only a hash of each device key is stored.
  The HTTPS certificate comes from a local certificate authority whose private
  key never leaves the PC. Pairing itself happens over plain HTTP on your LAN.
- Browser sign-in is OAuth 2.0 Authorization Code + PKCE — the distributed
  binary contains no secret. In Client Credentials mode the secret is entered
  by you and held in Windows Credential Manager.
- API keys, secrets, and tokens live in Windows Credential Manager via
  `keyring`, never in config files.
- Activity data (URLs, window titles) is sent to your chosen LLM provider when
  digests, matching, or Summarize run. In **MCP-only** mode, nothing leaves
  the machine unless you ask Claude about it.
- Screenshots are stored locally, are never sent to the LLM or to Halo, and
  are deleted after the retention period.
- Unpost deletes records in Halo. It only acts on entries TimeScribe posted,
  and for Quick Time it only removes a ticket that is a `Quick Time` ticket
  with no other time on it.
- The MCP server is intentionally read-only: it cannot create time entries,
  touch tickets, or modify anything.

## Roadmap

- Remote phone access through Cloudflare Tunnel + Access, with Halo sign-in
  as the gate
- ConnectWise Manage adapter (API-key model; `psa/adapter.py` is ready for it)
- Code-signing for AV-friendly distribution
- Remote ActivityWatch hosts (multi-machine capture) — config key exists
- Week view and richer reporting in the dashboard

## License

TimeScribe is MIT-licensed — see [LICENSE](LICENSE).

Bundled and dependent third-party software (including ActivityWatch under
MPL-2.0) is documented with full license texts and source pointers in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). This file also ships in the
installed application folder.

## Acknowledgements

- [ActivityWatch](https://activitywatch.net) (MPL-2.0) — bundled unmodified
  (v0.13.2) for window/AFK tracking. Source for the exact bundled build:
  https://github.com/ActivityWatch/activitywatch/releases/tag/v0.13.2
  Citation: Bjäreholt, E., & Bjäreholt, J. (2024). ActivityWatch [Computer
  software]. https://doi.org/10.5281/zenodo.4957165 (see
  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for BibTeX).
- [Segno](https://github.com/heuer/segno) (BSD-3-Clause) — bundled for the
  pairing QR code.
