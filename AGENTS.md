# handbook

A one-file dashboard that renders a markdown vault an AI agent maintains. Python stdlib only. No database, no sessions, no build step. Read this before changing anything.

## Run and check

- Run: `python3 server.py --vault /path/to/vault` (or `HANDBOOK_VAULT=...`). With neither, it runs the bundled `starter/`.
- Check: `python3 server.py --check` is the only test. It must print `ok` before any PR. CI runs it.
- Demo: `python3 server.py --vault starter` renders the bundled starter vault with no real data.
- Drift: `python3 lint.py --vault <vault>` reports broken links, missing paths, banned words, and task lines off format. The dashboard shows the count as the `drift` stat. The starter must stay clean; CI runs it.

## Where things are

- `server.py` everything. Order inside the file: config, cache, parsers, integrations (Attio, Linear, GitHub, git), markdown rendering, CSS, JS, modules, page assembly, HTTP handler, self-test.
- `lint.py` the drift check, config-driven (`lint` block in `handbook.json`).
- `starter/` a minimal vault that exercises every panel. Keep it working; the self-test renders it.
- `assets/` service icons served at `/assets/`. A vault can add its own in `<vault>/.dashboard/assets/` and they win over these.
- `DESIGN-SYSTEM.md` tokens and rules for the look. Follow it.

## Rules

- **Stdlib only.** No pip installs, no node. If it needs a dependency, it does not belong here.
- **One file stays one file.** Add a module as a function `m_name(c)` that returns HTML or `""`, register it in `SECTIONS` or `PANEL_MODULES`, and gate it with config if it is optional.
- **The vault is the only store.** Every write lands in a markdown file the owner can read and edit, through `save()` (atomic replace) under the one write lock. Never invent a state file, never call `write_text` on a vault file directly.
- **Integrations are gated.** Fetch only when `on("name")`; read secrets through `secret(ENV, path)`; return `{"error": ...}` instead of raising. The starter must render with every integration off.
- **Config seam.** Anything personal (names, repos, colors, playlists, paths) lives in `CONFIG` with a neutral default and is overridden by `<vault>/handbook.json`. Never hard-code a person, a client, or a machine path below `load_config()`.
- **Design tokens only.** Colors come from the CSS variables in `DESIGN-SYSTEM.md`. Accent marks live or interactive things only. No shadows, no gradients, no pure black or white.
- **No em dashes** in any string a person reads. No code comments unless the code cannot say it.
- **Security stance is explicit.** The server binds `127.0.0.1` and has no auth by design. Run it behind Tailscale or a similar private network. Do not add auth; do not widen the bind.
- **Test the parser you touch.** A change to a parser, a matcher, or a write path adds one assert to the self-test. Trivial one-liners need none.

## Vault contract

The dashboard reads these files, in the formats the starter shows:

- `Todo.md` sections per project, `- [ ] **Title** - description · source` lines, a `## Clocks` section of dated commitments.
- `90-Meta/Questions.md` with `## Open`, `## Q-NNN · title · asked YYYY-MM-DD` blocks ending in `**A:**`, and `## Inbox from dashboard`.
- `90-Meta/Assistant-Memory.md` with `## Tier-1 carry` and `### Bench`.
- `90-Meta/Capture-Log.md` (modification time only).
- `.dashboard/brief.html` optional, served at `/brief` when present.
- `90-Meta/Runs.md` optional run log, one line per scheduled run: `- YYYY-MM-DD HH:MM · run-name · ok · 3 files · 4 min` or `· failed: reason`. The rail shows the last status per run named in `config.runs`.
- `.dashboard/extras.py` optional private module; `anchor(config)` returns HTML for the day header. Never ship personal extras in this repo.
