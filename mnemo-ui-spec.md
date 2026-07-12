# mnemo UI — localhost web interface spec

> Additive feature for the existing mnemo codebase. A friendly, local-first web UI served by the FastAPI process at `localhost`, designed so a Tauri desktop shell later is packaging, not a rewrite. Written for handoff to opencode: backend tasks first (the UI's contract), then frontend tasks, each self-contained with acceptance criteria and a paste-ready prompt.

---

## 1. Product framing

**Audience:** developers and technical-adjacent users who installed mnemo and want to use it without a terminal or an MCP client.
**The UI's single job:** point mnemo at folders, watch it build understanding, then ask questions and *see exactly what it cost* — which files were used, which mode answered, how many tokens left the machine (often zero).
**Non-goals for v1 of the UI:** graph visualization (roadmap), multi-user anything, auth (localhost only), editing files, mobile layouts beyond "doesn't break."

### The three screens
1. **Library** — manage indexed folders, trigger indexing, watch live progress.
2. **Ask** — the chat surface: question → answer with cited sources and a synthesis-mode receipt.
3. **Status** — engine health (Ollama models, DB sizes, counts), synthesis-mode settings.

Navigation is a left rail with these three items. Ask is the default screen when at least one folder is indexed; Library is the default (and onboarding) when nothing is indexed yet.

---

## 2. Stack & repo placement

| Concern | Choice | Notes |
|---|---|---|
| Frontend | React 18 + TypeScript + Vite | widest contributor pool; Tauri-compatible later |
| Styling | Tailwind + CSS variables for tokens | tokens in `:root`, Tailwind consumes them |
| Data fetching | TanStack Query | caching, polling, mutation states for free |
| Streaming | native `EventSource` (SSE) | index progress + streamed answers |
| State | Query cache + small Zustand store for UI prefs | no Redux |
| i18n | tiny JSON dictionary (`es`, `en`), default from `navigator.language` | ES is first-class, not an afterthought |
| Build output | `packages/app/mnemo_app/static/` | FastAPI serves it; `uvx mnemo-app` opens the browser |

```
packages/app/
├── mnemo_app/            # existing FastAPI service
│   ├── api/              # routers (extended in UI-1)
│   └── static/           # built frontend (gitignored; CI builds it)
└── frontend/
    ├── index.html
    ├── src/
    │   ├── main.tsx, App.tsx, router.tsx
    │   ├── api/          # typed client, one module per resource
    │   ├── components/   # design-system primitives
    │   ├── features/
    │   │   ├── library/
    │   │   ├── ask/
    │   │   └── status/
    │   ├── i18n/         # en.json, es.json, useT()
    │   ├── stores/       # ui.ts (zustand: lang, theme, defaults)
    │   └── styles/       # tokens.css, globals.css
    └── vite.config.ts    # dev proxy → http://localhost:<port>
```

**Hard rule (Tauri-readiness):** the frontend is a pure API client. All backend calls go through `src/api/client.ts` with a single configurable `BASE_URL` (default `""` = same origin). No same-origin assumptions anywhere else, no server-rendered anything.

---

## 3. Design direction

The subject is *memory you can audit*: a quiet local archive that shows its work. The design should feel like a well-kept instrument — calm, precise, a little bookish — not a SaaS dashboard. One deliberate risk, spent in one place (see Signature).

### 3.1 Tokens (`styles/tokens.css`)

Dark-first (dev audience), light theme as a first-class variant. Palette is drawn from ink, paper, and archival violet — adjacent to the Positronica family (#434e74 / #889bd3) without copying it, so the project reads as yours in a lineup.

```css
:root[data-theme="dark"] {
  --bg:        #14161f;   /* ink — near-black with a blue-violet cast */
  --bg-raised: #1c1f2b;   /* cards, rail */
  --line:      #2b2f40;   /* hairline borders */
  --text:      #e8e6f0;   /* warm paper white */
  --text-dim:  #9a97ad;
  --accent:    #8f86d8;   /* archival violet — links, focus, active nav */
  --accent-ink:#14161f;   /* text on accent */
  --local:     #6fbf9a;   /* sage — anything that stayed on-machine */
  --cloud:     #d9a441;   /* amber — anything that left the machine */
  --danger:    #d96a6a;
}
:root[data-theme="light"] {
  --bg:        #f6f5f1;   /* unbleached paper */
  --bg-raised: #ffffff;
  --line:      #e2e0d8;
  --text:      #23222b;
  --text-dim:  #6e6c7a;
  --accent:    #5b51b8;
  --accent-ink:#ffffff;
  --local:     #2e7d5b;
  --cloud:     #a06a10;
  --danger:    #b23a3a;
}
```

The **local/cloud color pair is semantic and sacred**: sage always means "stayed local," amber always means "went to cloud." It appears in mode badges, the receipt, progress states, and the status screen. This is the visual encoding of the product thesis; never reuse these two hues decoratively.

### 3.2 Type

- **Display / headings:** `Fraunces` (serif, slightly bookish — the "archive" voice). Weights 500–600 only, used sparingly: screen titles and the empty-state headline.
- **Body / UI:** `Inter`, 14px base, 1.55 line height.
- **Data / paths / receipt:** `JetBrains Mono`, 12–13px. File paths, token counts, and the receipt are always mono — numbers and paths are the material of this product and should look like it.
- Scale: 13 / 14 / 16 / 20 / 28. Nothing larger; this is an instrument, not a landing page.

Self-host both via `@fontsource/*` packages — no network font fetch; the app must work fully offline.

### 3.3 Signature element: the receipt

Every answer ends with a **receipt** — a mono-type block styled like a till receipt (hairline top border, dotted leader lines between label and value):

```
── receipt ────────────────────────────
mode                     local  ● sage
files consulted              4
chunks sent                  6
tokens · local            1,842
tokens · cloud                0
time                      3.2s
───────────────────────────────────────
```

When mode is `cloud` or an `auto` escalation, the cloud token line renders in amber with `● cloud` on the mode row. The receipt is collapsible but **rendered by default** — cost transparency is the product's whole argument, so the UI leads with it. This is the one memorable thing; everything around it stays quiet.

### 3.4 Motion & quality floor

- One orchestrated moment: on the Ask screen, sources fade-slide in *before* the answer streams — retrieval visibly precedes synthesis, which teaches the architecture without a diagram.
- Otherwise: 120–160ms ease-out on hovers/expands only. Respect `prefers-reduced-motion` (disable the sequence, keep opacity fades).
- Quality floor, not announced: keyboard focus visible everywhere (`--accent` 2px outline), all interactive elements reachable by tab, `aria-live="polite"` on streaming regions, contrast ≥ 4.5:1 for text in both themes, works at 1024px width.

### 3.5 Voice (copy rules)

Sentence case everywhere. Buttons say what they do: "Index folder," "Ask," "Remove folder." Errors state what happened and the next action, never apologize: *"Ollama isn't reachable at localhost:11434. Start it, then retry."* Empty states are invitations: *"Nothing indexed yet. Add a folder and mnemo will read it once, locally."* All strings live in the i18n dictionaries — no hardcoded English in components.

---

## 4. API contract (backend work, UI-1)

The UI needs four additions to the existing FastAPI app. Everything is JSON over `localhost`; no auth. Adapt names to the real repo, keep the shapes.

### 4.1 Folder management

```
GET  /api/folders
→ 200 [{ "id": "f_ab12", "path": "/home/j/notes", "status": "indexed",
         "files_total": 412, "files_indexed": 412, "files_error": 3,
         "last_indexed_at": "2026-07-05T14:02:11Z" }]

POST /api/folders            { "path": "/home/j/projects/venus" }
→ 201 { "id": "f_cd34", ... }        # validates path exists & is a directory
→ 422 { "error": "not_a_directory", "detail": "..." }

DELETE /api/folders/{id}     # removes folder + its chunks/nodes from stores
→ 204
```

### 4.2 Native directory browsing (solves the browser folder-picker gap)

```
GET /api/fs/browse?path=/home/j
→ 200 { "path": "/home/j", "parent": "/home",
        "dirs": [{ "name": "notes", "path": "/home/j/notes" }, ...] }
```

Server-side rules: directories only (never list files — reinforces that mnemo reads, the user doesn't upload); resolve symlinks with `realpath`; refuse paths outside the user's home unless `allow_outside_home = true` in config; hide dotdirs unless `?hidden=true`.

### 4.3 Indexing + live progress (SSE)

```
POST /api/folders/{id}/index          # idempotent; 409 if already running
→ 202 { "job_id": "j_9f" }

GET /api/jobs/{job_id}/events         # text/event-stream
event: progress
data: { "done": 118, "total": 412, "current": "notes/china-trip.md",
        "skipped_unchanged": 61, "errors": 2 }
event: file_error
data: { "path": "notes/broken.pdf", "error": "extract_failed" }
event: done
data: { "done": 412, "total": 412, "skipped_unchanged": 61, "errors": 2,
        "seconds": 184.2 }
```

`skipped_unchanged` is surfaced deliberately — it's the summarize-once guarantee made visible ("mnemo skipped 61 files it already understood").

### 4.4 Ask (extend the existing `/ask`)

```
POST /api/ask
{ "question": "what did I conclude about the Alipay paradox?",
  "mode": "auto" | "local" | "cloud" | null,     # null → config default
  "stream": true }

SSE stream:
event: sources        # emitted FIRST, before any answer tokens
data: { "files": [{ "file_id": "...", "path": "notes/china-trip.md",
                    "summary": "...", "why": "mentions Alipay; topic: fintech" }],
        "relationships": ["china-trip.md —ABOUT→ fintech —ABOUT— venus-pitch.md"] }
event: token
data: { "text": "You framed it as..." }
event: receipt        # emitted LAST
data: { "mode": "local", "escalated": false, "files": 4, "chunks": 6,
        "tokens_local": 1842, "tokens_cloud": 0, "seconds": 3.2 }
event: error
data: { "error": "synth_unavailable", "detail": "Ollama not reachable ..." }
```

Non-streaming fallback (`stream:false`) returns `{ answer, sources, receipt }` in one body.

### 4.5 Status & settings

```
GET /api/status
→ { "ollama": { "reachable": true, "models": ["bge-m3", "qwen2.5:7b"] },
    "counts": { "files": 1204, "chunks": 18344, "entities": 512, "topics": 88 },
    "db_bytes": { "vectors": 412000000, "graph": 8100000 },
    "synth": { "backend": "auto", "cloud_configured": false } }

GET  /api/settings          → current [synth] section (never returns key values)
PUT  /api/settings          { "backend": "auto", "auto": { ... } }   # persists to mnemo.toml
```

Settings endpoint edits only the `[synth]` table; API keys are referenced by env-var name and never round-trip through the UI.

---

## 5. Screen specs

### 5.1 Library

**Layout:** page title ("Library"), primary button "Add folder," then a list of `FolderCard`s.

**FolderCard** (raised bg, hairline border, 16px padding):
- Row 1: folder name (body, medium) + full path (mono, dim, truncated middle).
- Row 2: status line. States:
  - `indexed`: `412 files · 61 skipped as unchanged · indexed 2h ago` (mono, dim); dot in `--local` sage.
  - `indexing`: slim progress bar (accent fill) + `118 / 412 · china-trip.md` live from SSE; "Cancel" ghost button.
  - `error`: `--danger` dot + `3 files failed` as a link expanding an inline mono list of `file_error` events with reasons.
  - `pending` (added, never indexed): "Index now" primary button on the card.
- Kebab menu: Re-index (full), Remove (confirm dialog states exactly what's deleted: "Removes 412 summaries, 6,120 chunks, and graph links. Your files are untouched.").

**Add-folder flow:** button opens a modal `DirectoryPicker` — breadcrumb of the current path (each crumb clickable), list of subdirectories from `/api/fs/browse`, a mono path input at top for paste-and-go, footer buttons Cancel / "Add this folder". On add: card appears in `pending`, and if it's the first-ever folder, indexing starts automatically (onboarding shouldn't require a second click).

**Empty state (first run):** centered, Fraunces headline "Nothing indexed yet," one sentence — "Add a folder and mnemo will read it once, locally. After that, questions are nearly free." — and the Add folder button. This doubles as onboarding; no tour, no tooltips.

### 5.2 Ask

**Layout:** conversation column (max-width 760px, centered), composer pinned at bottom.

**Composer:** auto-growing textarea (Enter submits, Shift+Enter newline), placeholder "Ask about your files…", and a **ModeSelect** on the right — a segmented control: `Auto · Local · Cloud`, with the active segment tinted sage (local), amber (cloud), or neutral with a small split sage/amber dot (auto). Cloud segment disabled with tooltip "No API key configured — set one in Status" when `synth.cloud_configured` is false. Default from settings; per-question override is the point of the control.

**Answer block anatomy (top to bottom):**
1. The question, right-aligned, dim.
2. **SourcesRow** — appears first (the orchestrated moment): horizontal row of `SourceChip`s, each showing filename (mono) and, on hover/tap, a popover with the file summary and the `why` line. If `relationships` is non-empty, a final chip "graph ↗" expands one or two relationship lines in mono beneath the row.
3. **Answer** — streamed markdown (`token` events appended; `aria-live="polite"`). Render markdown with a hardened renderer (no raw HTML).
4. **Receipt** — per §3.3, collapsible, open by default. When `escalated:true`, prepend one plain sentence above it: "Auto escalated to cloud for this one — it spanned 5 files."
5. Utility row (ghost, dim): copy answer · re-ask locally / re-ask with cloud (swaps mode, resubmits same question).

**States:** while sources are loading, show three shimmer chips; if `error` event arrives, replace the pending answer with an `ErrorCard` (what happened + the fix + "Retry" button). If a question is asked with zero indexed folders, short-circuit client-side to a pointer at the Library.

**History:** session-only (in-memory). Persisted conversation history is out of scope for v1; note it in the roadmap.

### 5.3 Status

Three stacks:

1. **Engine** — Ollama reachability (sage dot / danger dot + fix-it line with the configured endpoint), the two configured model names (mono), counts row (`1,204 files · 18,344 chunks · 512 entities · 88 topics`, mono), DB sizes humanized.
2. **Synthesis** — the settings form: backend radio (`return_only` hidden here — it's an MCP concern — show `local / cloud / auto`), auto-mode thresholds (two number inputs + intent-keywords tag input), cloud section showing provider, model (text input), and key env-var name with a read-only "detected ✓ / not set" check. Save button calls `PUT /api/settings`; success toast "Settings saved."
3. **About** — version, links (repo, docs, MCP setup guide), language toggle (ES/EN), theme toggle.

### 5.4 App shell

Left rail, 220px, `--bg-raised`: wordmark "mnemo" in Fraunces at top; nav items Library / Ask / Status with a 2px accent left-bar on the active item; bottom of rail shows a permanent one-line **privacy footer** in mono, dim: `● local — nothing sent` (sage) that flips to `● cloud configured` (neutral) when a key is set. Content area scrolls independently. Below 1024px the rail collapses to icons.

---

## 6. Frontend architecture details

- **Typed API client** (`api/client.ts`): thin `fetch` wrapper + `sse(url, handlers)` helper wrapping `EventSource` with reconnect (retry once, then surface error). All response shapes defined in `api/types.ts` mirroring §4 exactly.
- **Queries:** `useFolders()` (poll every 5s only while any folder is `indexing`), `useStatus()` (poll 30s on Status screen only), mutations for add/remove/index/settings with optimistic UI on remove.
- **Ask flow:** a `useAsk()` hook owns the SSE lifecycle and exposes `{ phase: 'idle'|'sources'|'streaming'|'done'|'error', sources, answerText, receipt, error }` — components render purely from this state machine, no imperative DOM.
- **i18n:** `useT()` reads the Zustand `lang`; dictionaries are flat keys (`"ask.placeholder"`), ES and EN complete or CI fails (script diffs key sets).
- **Testing:** Vitest + Testing Library. Must-cover: the `useAsk` state machine against a mocked SSE (sources→tokens→receipt, and the error path), DirectoryPicker navigation, receipt rendering for local vs cloud vs escalated, mode-select disabled state.

---

## 7. Delegatable tasks

Order: UI-1 (backend contract) → UI-2 (scaffold + design system) → UI-3/UI-4/UI-5 (screens, parallelizable after UI-2) → UI-6 (polish gate). Each prompt tells opencode to inspect the repo first.

### UI-1 — Backend endpoints for the UI
**Touches:** `mnemo_app/api/*`, job runner, config
**Done when:** all §4 endpoints exist and match the shapes; indexing runs as a background job emitting SSE per §4.3 including `skipped_unchanged`; `/api/ask` streams `sources` → `token`* → `receipt` (or `error`) and supports `stream:false`; `/api/fs/browse` enforces the path rules; settings PUT persists `[synth]` to the TOML without touching other tables; pytest covers browse path-escape refusal, job lifecycle, and the ask event order.
**Prompt:**
> Inspect the FastAPI app and the synthesis-modes feature. Implement the API contract in §4 of this spec exactly: folder CRUD, `GET /api/fs/browse` (dirs only, realpath, refuse outside $HOME unless configured), `POST /folders/{id}/index` spawning a background job with an SSE event stream (`progress`/`file_error`/`done`, include `skipped_unchanged`), streaming `POST /api/ask` that emits `sources` first, then `token` events, then `receipt` (mode, escalated, files, chunks, tokens_local, tokens_cloud, seconds), plus `/api/status` and get/put `/api/settings` (synth section only, never key values). Add tests for path-escape, job lifecycle, and ask event ordering with a faked synthesizer.

### UI-2 — Frontend scaffold + design system + shell
**Touches:** `packages/app/frontend/*`, FastAPI static mount
**Done when:** Vite+React+TS+Tailwind builds into `mnemo_app/static` and FastAPI serves it (dev proxy works); tokens.css implements §3.1 both themes; Fraunces/Inter/JetBrains Mono self-hosted; primitives built and storybook-style demoed on a hidden `/dev` route: Button (primary/ghost/danger), Card, SegmentedControl, ModeBadge (sage/amber/auto), ProgressBar, Toast, Modal, ErrorCard, Receipt (per §3.3 with dotted leaders), SourceChip with popover; app shell per §5.4 with routing, i18n plumbing (`useT`, es/en dictionaries), theme + language toggles persisted in Zustand.
**Prompt:**
> Scaffold `packages/app/frontend` per §2: Vite + React 18 + TS + Tailwind + TanStack Query + Zustand, building into `mnemo_app/static`, with a dev proxy to the FastAPI port. Implement the §3.1 token system (dark + light), self-host Fraunces/Inter/JetBrains Mono via fontsource, and build the primitive components listed in this task — including the Receipt component exactly per §3.3 (mono, dotted leaders, sage/amber semantics) and SourceChip with summary popover. Add the app shell (§5.4): 220px rail, wordmark, nav, privacy footer, icon-collapse under 1024px. Wire i18n (`useT`, complete es+en dictionaries, CI key-diff script) and visible keyboard focus everywhere. Demo all primitives on a `/dev` route.

### UI-3 — Library screen
**Touches:** `features/library/*`
**Done when:** folder list renders all four card states; SSE progress updates the indexing card live (bar, counter, current file) and surfaces `skipped_unchanged` in the done state; DirectoryPicker modal browses via the API with breadcrumbs + paste-a-path input; add-first-folder auto-starts indexing; remove shows the explicit-consequences confirm; empty state per §5.1; tests cover the picker navigation and card state transitions from mocked SSE.
**Prompt:**
> Build the Library screen per §5.1 using the UI-2 primitives and the §4.1–4.3 endpoints. FolderCard with `pending/indexing/indexed/error` states, live SSE progress (done/total, current file, cancel), expandable file-error list, kebab with re-index and remove (confirm dialog must state counts of summaries/chunks/graph links being deleted and that files are untouched). DirectoryPicker modal: breadcrumbs, dir list from `/api/fs/browse`, mono path input. First folder added → indexing starts automatically. Implement the §5.1 empty state. Tests: picker navigation, state transitions from a mocked event stream.

### UI-4 — Ask screen
**Touches:** `features/ask/*`
**Done when:** composer with Enter-to-send and ModeSelect (cloud disabled + tooltip when unconfigured); `useAsk` state machine drives the full sequence — shimmer chips → SourceChips (with why-popovers and the graph chip) → streamed markdown answer (`aria-live`) → Receipt open by default with correct sage/amber semantics and the escalation sentence; error event renders ErrorCard with retry; re-ask-with-other-mode utility works; zero-index short-circuit points to Library; the §3.4 sources-before-answer motion implemented and disabled under reduced-motion; tests cover the state machine happy path, error path, and escalated receipt.
**Prompt:**
> Build the Ask screen per §5.2. Implement `useAsk()` owning the SSE lifecycle with phases `idle→sources→streaming→done|error`, rendering: question (right, dim) → SourcesRow (chips with summary/why popover, optional graph-relationships chip) → streamed markdown (hardened renderer, no raw HTML, aria-live polite) → Receipt (§3.3, open by default; if `escalated`, prepend the plain-language sentence). Composer: autogrow textarea, Enter submits, ModeSelect segmented control with sage/amber/auto tinting and disabled-cloud tooltip driven by `/api/status`. Add copy-answer and re-ask-with-mode utilities, the zero-folders short-circuit, and the sources-fade-in-before-answer sequence (respect prefers-reduced-motion). Tests: state machine over mocked SSE for happy, error, and escalated cases.

### UI-5 — Status screen
**Touches:** `features/status/*`
**Done when:** engine stack shows Ollama reachability with fix-it copy, models, counts, DB sizes; synthesis form edits backend + auto thresholds + intent tags + cloud model with env-var detected-check, saving via PUT with toast; about stack has version, links, language + theme toggles; the rail privacy footer reacts to `cloud_configured`; tests cover the settings round-trip and the unreachable-Ollama state.
**Prompt:**
> Build the Status screen per §5.3 against `/api/status` and `/api/settings`: engine health (reachability dot with actionable fix line, model names in mono, counts row, humanized DB sizes), synthesis settings form (local/cloud/auto radio, auto thresholds, intent-keyword tag input, cloud model text input, read-only API-key env detection), about section with version/links and the language/theme toggles. Wire the rail privacy footer to `cloud_configured`. Toast on save. Tests: settings round-trip with a mocked API, Ollama-down rendering.

### UI-6 — Polish gate (a11y, i18n, offline, release)
**Done when:** keyboard-only walkthrough of all three screens succeeds with visible focus; both themes pass 4.5:1 on text (checked with a contrast script); ES and EN dictionaries verified complete; app loads and functions with network disconnected (fonts local, no external requests — assert in a test that no request leaves localhost); `uvx mnemo-app` opens the browser to the UI; README gains a UI section with two screenshots (dark Ask with receipt, Library indexing) and a line on the Tauri roadmap.
**Prompt:**
> Run the release gate: keyboard-only navigation audit (fix any trap or missing focus style), automated contrast check on both themes, i18n completeness check wired into CI, and a test asserting the built app makes zero non-localhost requests (fonts must be bundled). Make `uvx mnemo-app` open the default browser to the UI on start. Add a README "UI" section (EN + ES) with screenshots of the Ask screen (receipt visible) and Library mid-index, plus a roadmap note on the Tauri desktop shell.

---

## 8. Roadmap (explicitly out of v1)

- **Tauri shell**: wrap this exact frontend, Python as sidecar; native folder dialog replaces DirectoryPicker; tray icon for the future watcher. Good first contributor milestone.
- **Graph explorer**: sigma.js view over Kùzu — a fourth nav item, demo gold, scope sinkhole; only after v1 ships.
- **Persisted conversations**, saved answers, export.
- **Cost meter**: cumulative cloud-token counter on Status ("$0.12 this month"), fed by receipts.
