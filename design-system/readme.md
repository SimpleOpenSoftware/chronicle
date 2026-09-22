# Chronicle Design System

## Current production theme

The WebUI uses the Espresso palette in `backend/webui/chronicle-espresso-preset.js`.
Its copy in `extras/speaker-recognition/webui/chronicle-espresso-preset.js` must stay
identical. Tailwind gray utilities resolve to warm espresso neutrals and blue
utilities resolve to terracotta. The original exported specimens below predate
this palette; their literal blue/gray values are historical, not production defaults.

`backend/webui/src/index.css` defines the shared workspace roles in both themes:
`--chronicle-navigation` for navigation, `--tape-paper` for workspaces,
`--tape-paper-raised` for cards and inputs, `--tape-ink` for primary text,
`--tape-activity` for readable secondary text, `--tape-line` for control/card borders,
and `--tape-focus`/`--tape-selected` for focus and selection. Use these roles when
combining recording search with Timeline surfaces. Do not create another palette.
Shared icon actions use a 44px minimum hit area; visible glyphs remain compact.


A design system **reverse-engineered from the Chronicle admin dashboard code** — the
project was AI-generated with inline Tailwind and never built off a system, so this
captures the patterns that already recur across the product and turns them into reusable
tokens + components.

## Sources
- Codebase: `chronicle/backend/webui/` (React + Vite + Tailwind, dark-mode via `dark:` class).
  - Pages read: `pages/WakeWordLab.tsx`, `pages/DataAudit.tsx`, `components/layout/Layout.tsx`, `components/dataAudit/SpeakerConfidencePanel.tsx`, `components/finetuning/EnrollmentCandidates.tsx`.
  - Config: `tailwind.config.js` (default theme, no palette overrides), `src/index.css` (`font-family: system-ui, sans-serif`).
- The tokens are the **default Tailwind palette/scale** as actually used — not a bespoke palette. The product's signature look is the **dark** theme, so dark is the default on `:root`; a `[data-theme="light"]` scope carries the `dark:`-variant light mode.

> No Figma was provided. This was extracted from code, which is higher-fidelity than screenshots.

## CONTENT FUNDAMENTALS
- **Voice:** precise, operator-facing, explains *why*. Microcopy teaches the decision, e.g. tooltips distinguish "Wake / Not / Delete" and warn "Don't mark an overlapping word 'Not' — it contains the real sound." Descriptions state consequences ("fires live to gather review data without dispatching").
- **Person:** second person imperative for actions ("Decide what the audio is", "tick them to override"); third person for system state.
- **Casing:** sentence case everywhere — headings, buttons, labels. Not Title Case. Product nouns keep their hyphen/caps: "Wake-Word Lab", "Data Audit".
- **Numbers & code:** monospace for scores (`0.996`), model files (`hey_hermes_f.onnx`), timestamps, counts.
- **Punctuation:** em dashes for asides; "·" (middot) as an inline separator ("thr 0.9 · patience 2", "3s · smart_turn"); curly quotes around wake words ("hey hermes").
- **Emoji:** essentially none in UI chrome; a rare "⚠" prefixes an inline caution. Do not add emoji.
- **Tone example:** "Only the segments you relabelled by hand are candidates, gated for quality (≥ 3s, no cross-talk, deduped)."

## VISUAL FOUNDATIONS
- **Theme:** dark-first. App background `#111827` (gray-900); header/sidebar/main-content are raised `#1f2937` (gray-800) cards; inputs/chips/inner tiles drop back to `#111827` on the raised surface. Hairline borders `#374151` (gray-700) do most of the separation — **borders, not shadows**, define structure.
- **Color:** neutral gray ramp + a single brand **blue** (`#2563eb`) for the one primary action / active nav / active tab. Status is a small semantic set used as **soft translucent chips** (fill at ~40% alpha + a light 300-level text): green = good/verifier, red = negative/error, amber = collect-only/warning, blue = info/"missed", purple = "also fired" suggestion.
- **Type:** `system-ui`. Tailwind default scale (12→30px). Weights 500/600/700. Mono (`ui-monospace`) for all code-like data. Page titles `-0.01em` tracking; uppercase group labels get `0.08em`.
- **Spacing:** 4px grid; layout is **flex/grid with `gap`**, not margins. Section gaps 24/32px; card padding 16px; content card padding ~24–28px.
- **Radii:** 4 chips · 6 buttons/rows · 8 cards/inputs/tabs · 12 large per-item panels · full pills. Corners are consistently soft, never sharp.
- **Elevation:** `shadow-sm` on base cards; `shadow-lg` on modals; a heavier pill shadow on the floating volume-boost control; a menu shadow on dropdowns. Otherwise flat.
- **Backgrounds:** solid fills only — **no gradients, no imagery, no textures, no blur**. (Avoid adding any.)
- **Motion:** minimal — `~200ms` ease color/background transitions on hover; a chevron rotate on expand; an `animate-pulse` only on a live "primed, say it now" banner. No bounces, no entrance animations.
- **States:** hover = one step lighter surface (`gray-700→600`) or darker accent (`blue-600→700`); nav hover = faint sunken fill. Disabled = 40% opacity + not-allowed. No shrink-on-press.
- **Cards:** bordered rounded rectangles on the raised surface; the "hub tile" variant highlights with a blue border + `blue-900` tinted fill when active. Left-border-accent cards are NOT used.

## ICONOGRAPHY
- The app uses **[Lucide](https://lucide.dev)** (`lucide-react`), stroke style, `stroke-width: 2`, sized 12–32px (h-3…h-8), colored via `currentColor` (muted grey by default, blue for accented/section icons).
- This system references Lucide via its browser UMD build (`unpkg.com/lucide`) in cards and the UI kit, and every component takes icon nodes as props rather than hard-coding glyphs — so consumers pass the real Lucide icon. Common glyphs: `music` (brand mark), `sparkles` (Data Audit), `target` (Wake-Word Lab), `mic`, `radio`, `shield-check`, `eye`, `gauge`, `refresh-cw`, `arrow-right-left`, `copy-x`, `volume-2`, `trash-2`, `check`, `x`, `alert-triangle`.
- **No PNG/SVG icon assets and no icon font** live in the repo — everything is Lucide, so there was nothing to copy into `assets/`. Unicode "·", "›", "⚠" appear as inline typographic marks.
- **Logo:** the product has **no logo asset**; the wordmark "Chronicle Dashboard" is set in plain type beside a Lucide `music` icon. No mark was invented — render the name in type wherever a logo would go.

## Intentional additions
- `StatCard`, `HubCard`, `CollapsibleSection`, `Breadcrumb` are named/extracted from repeated inline patterns (the code has no shared primitives folder). They match real usage 1:1; names are ours.

## Index / manifest
- `styles.css` — entry; `@import`s `tokens/{colors,typography,spacing,effects}.css`.
- `guidelines/*.html` — foundation specimen cards (Colors, Type, Spacing groups).
- `components/`
  - `core/` — Button, IconButton, Badge, Card, StatCard, Tabs
  - `forms/` — Input, Select, Checkbox
  - `navigation/` — NavItem, Breadcrumb
  - `feedback/` — Alert, Modal
  - `data/` — CollapsibleSection, HubCard
- `ui_kits/chronicle-dashboard/` — interactive Data Audit hub → Wake-Word Lab recreation.
- `SKILL.md` — portable skill wrapper.

## Setup note
The component cards and the UI kit load the generated `_ds_bundle.js`. It is produced only
once this project's **File type is set to "Design System"** (Share menu). Until then, foundation
(token) cards render but component cards will look empty.
