# TimeScribe Roadmap

Priorities set 2026-07-21. Working branch: `dev`; `main` holds releases.

## Phase 1 — Digest quality & ticket matching (top pain)

- **Profile → client mapping config.** The Edge profile name is already the
  primary attribution signal; make the mapping explicit and editable so
  renames/aliases don't degrade attribution.
- **Per-client keyword hints.** Config list like `"BornGood": ["NWL", "borngood.com"]`
  fed into the digest prompt so window titles land on the right client.
- **Smarter ticket matching.** Feed the matcher the last ~30 days of posted
  entries (client + keywords → ticket patterns) so recurring work maps to the
  right ticket without guessing. Expose the same matching through the MCP
  prompt path so MCP-only users get it without an API key.
- **Prompt tuning** based on observed misattributions.

## Phase 2 — UI modernization & accessibility (team-rollout priority)

- Consistent design system: typography, spacing, components; light + dark.
- Accessibility: keyboard navigation end-to-end, ARIA roles/labels, visible
  focus states, adequate contrast, reduced-motion support, larger hit targets.
- Simplify first-run: Setup card walks through Halo → AI provider → done.

## Phase 3 — Calendar ingestion

- Outlook/Teams meetings become pre-made draft entries (subject, time range,
  attendees → client mapping).
- Evaluate ingestion routes: MS Graph device-code OAuth vs. Halo appointments
  vs. ICS. Decide after Phase 1.

## Backlog

- **ConnectWise Manage adapter** (speculative — no concrete user yet).
  `PSAAdapter` stays the seam: implement auth (company id + member API keys),
  `create_time_entry` → `POST /time/entries`, ticket listing. Build when a
  real CW user appears.
- Unbilled-work finder (activity vs. posted-time diff, daily).
- Live tray timer against a ticket.
- Weekly per-client hours summary.
- Signed binaries + auto-update (removes SmartScreen/EPERM class of issues).

## Recently shipped (context)

- Time entries post as Halo ticket Actions; quick time via /TimesheetEvent.
- Repost + delete + Drafts/Posted sub-tabs.
- Settings → Logs viewer; full request logging on Halo POSTs.
- OAuth resilience: stale/revoked token auto-clear with clear reconnect message.
- MCP server: parsing-guide instructions/tool/resource + summarize_day prompt.
