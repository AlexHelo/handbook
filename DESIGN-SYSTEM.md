# Handbook dashboard — design system (v1, 2026-08-28)

The question that governs every choice: **if Teenage Engineering built an agentic OS panel, what would it look like?** (the brief: minimalist, incredibly functional, made with care.) Design read: a personal control panel for its builder-owner, hardware-panel language, high function density with calm rhythm. Dials: variance 3, motion 2, density 6.

Mobbin findings this system is built on (researched 2026-08-28): Better Stack's stat blocks (tiny label over big mono value), the OpenAI status page's one-plain-sentence calm, Qatalog's hairline tables with generous rows, Railway's subtle dot-grid canvas, incident.io's orange-on-neutral. Anti-references: Wrike/ClickUp/Asana chip-avatar-badge clutter — none of that, ever.

## Config seam (shareability rule)

All identity lives in ONE Python dict at the top of `server.py` (`CONFIG`, neutral defaults) overridden by `<vault>/handbook.json` (owner, repos, colors, playlist, paths). Everything below is generic.

## Tokens

CSS variables on `:root`, dark under `@media (prefers-color-scheme: dark)`. Both modes shipped and tested.

| token | light | dark |
|---|---|---|
| `--chassis` (page) | `#E8E7E3` | `#161615` |
| `--panel` | `#F4F3F0` | `#1E1E1C` |
| `--panel-2` (inset/wells) | `#ECEBE7` | `#191918` |
| `--ink` | `#1B1B19` | `#E9E8E4` |
| `--ink-2` (secondary) | `#6E6D68` | `#8B8A85` |
| `--line` | `#C9C8C2` | `#33332F` |
| `--line-soft` | `#DBDAD5` | `#282825` |
| `--accent` | `#FF4A00` | `#FF5A14` |
| `--ok` | `#3D7A4A` | `#5FA36E` |
| `--warn` | `#B0790A` | `#D0972B` |

- **Accent discipline:** orange marks *live/interactive/now* only — the active tab key, primary buttons, the one thing that needs you today. Never decorative. `--ok`/`--warn` only for real semantic state (a clock overdue, an inbox item waiting).
- No pure black/white, no gradients, no drop shadows anywhere. Depth = hairlines + the two panel tones.
- Chassis texture: an ultra-subtle dot grid on the page background only (`radial-gradient` dots at ~24px, opacity so low it reads as paper grain). Panels are flat.

## Type

- **UI/labels/headlines:** `Space Grotesk` (Google Fonts, weights 400/500/700), fallback `system-ui, sans-serif`.
- **Data/numbers/timestamps/paths/counts:** `IBM Plex Mono` (400/500), fallback `ui-monospace, monospace`. Every number on the page is mono. No exceptions.
- Scale (px): 28 day-anchor · 17 section head · 14.5 body/task titles · 12.5 secondary · 11 labels. Labels are the only uppercase: 11px mono, `letter-spacing: .08em`, `--ink-2`. Used solely for functional labels (panel names, column names, states) — never as decoration.
- No italics, no serifs, no emojis anywhere in the chrome (house law). Vault content renders as-is.

## Shape & layout

- **Radius rule (documented, applied everywhere):** chassis and panels are sharp (`border-radius: 0`); interactive elements — buttons, tab keys, inputs, checkboxes — are `6px` (hardware keys on a flat faceplate). Nothing else is rounded.
- Grid: content max-width `1120px`, centered; 12-col with `20px` gutters; panels separated by `1px solid var(--line)`, joined edge-to-edge where grouped (shared borders like a machined faceplate, not floating cards with gaps).
- Section rhythm inside panels: `16px` padding, hairline `--line-soft` between rows, row height ≥ `40px`.

## Components

- **Top strip (the faceplate header):** left — wordmark `handbook` lowercase Space Grotesk 700 + tiny mono date/time. Right — two run stat-blocks: `BRIEF 07:15 ✓` / `SYNC 16:14 ✓` style (label + mono time of last run, read from the git log or file mtimes; `--warn` if a run is overdue). This is real state, not decoration.
- **Tab keys:** the 5 tabs as a row of hardware keys — bordered rectangles sharing edges, 6px outer radius on the group ends, active key = `--accent` background with chassis-colored text, inactive = panel tone, `:active` presses down 1px. Keyboard: `1–5` switch tabs.
- **Stat block:** 11px mono label over a large mono value (22–28px). For clocks: `CLIENT · 3d` with `--warn` when ≤ 2 days or overdue.
- **Task row:** checkbox (6px radius, accent when checked) · title 14.5px `--ink` · continuation lines collapsed behind a plain `+N lines` toggle · provenance suffix right-aligned 11px mono `--ink-2`. No badges, no dots, no per-project colors — the group heading carries the project.
- **Question entry (Questions tab):** the entry text rendered with minimal markdown (bold, lists, links as plain text) at body size, then a plain `<textarea>` (panel-2 well, hairline border, mono 13px) + one accent `answer` button that writes the `**A:**` line. This tab is where Alex answers the queue — it gets first-class care.
- **Inbox composer:** full-width textarea well + one accent button. Below it, "not picked up yet" items as plain rows with a `--warn` dot (real state: waiting for capture-sync).
- **Buttons:** primary = accent fill, chassis text, 6px, mono 12px label lowercase; secondary = panel tone + hairline. `:active` translates 1px down. All labels one word where possible: `answer`, `send`, `close`.
- **Empty states:** one quiet sentence in `--ink-2`, nothing else.

## Motion

Functional only: 120ms ease-out on tab switch and row expand, the 1px key press. Nothing loops, nothing floats, nothing on scroll. `prefers-reduced-motion` collapses even these.

## Banned (from the taste skill + house law)

Em dashes in chrome copy (vault content exempt) · emojis · icons doing decoration (a glyph must carry state or action) · colored chips/badges/avatars · drop shadows · gradients · cards floating on gray · decorative dots · section-number eyebrows · "scroll" cues · spinners (skeleton wells instead) · any string a tired human can't parse cold.

## v6 amendments (2026-08-28, from the owner's live review)

- **Left rail replaces the top tab strip** — a vertical module rack of keys (name + mono count + the keyboard digit in tiny mono — a functional shortcut hint, which is why it escapes the numbering ban). Collapses back to a top strip under ~900px.
- **Icons, narrowly allowed:** inlined Phosphor (MIT) paths, 12-14px, `--ink-2` stroke, only where a glyph carries source or state (task provenance marks: meeting, run, dashboard, phone, mail). Never decoration; the tooltip always carries the words.
- **Boldness pass:** day anchor ~36px/700, stat values ~28px mono. TE boldness is functional: the biggest thing on screen is the thing that matters most today.
- **Countdown stat pattern:** value = time remaining ("2h 04m"), caption = what fires and when ("next brief · 07:15"); late flips to amber "late 3h".
- **Diff rendering (handbook Changes view):** additions on ~12% `--ok` tint, deletions on a `--diff-del` red tint derived per theme, hunk headers mono `--ink-2`, file headers as panel heads.

## Accessibility

WCAG AA contrast in both modes (check `--ink-2` on `--panel`); visible focus rings (2px accent outline, offset 2); every control keyboard-reachable; `aria-label` on icon-only controls.

## v7 amendments (2026-09-04, the design pass after six seat reviews)

- **Spacing scale.** The day header panel gets 26px top and 24px sides; dense panels keep 16px. One padding for everything was the root of "spacing feels off".
- **Control bar.** Filters leave the panel header. `.bar` under the header holds the segmented keys on the left and project `.pills` on the right; the header keeps only the label and the count.
- **Pills.** `.pill` for filters (accent when on) and `.pill.tag` for status (stage, "no next step" in `--warn`, won in `--ok`). Pills never carry an icon.
- **Sort per group.** Each project block has its own `.gsort` select in the group header, remembered per project. There is no global sort.
- **Done group.** Done lines leave their project and collapse into one `Done` block at the end, with the archive button in its header.
- **Row actions.** `comment` and `more` are hover-only on pointer devices, always visible on touch. Comment is the action that talks to the agent, so it sits first.
- **Provenance tiles** are desaturated at rest and full-colour on hover so they stop reading as status.
- **Deals list.** The pipeline is one sorted list, not columns: stage pill, name, value, days in stage, next step on a second line, lost deals hidden behind a `show lost` pill. A `.totals` strip above: in play, value when one currency, oldest, won.
- **Phone.** Bottom keys drop their counts and never clip; the sidebar stacks under the board below 1280px.
