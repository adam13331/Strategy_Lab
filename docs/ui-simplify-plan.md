# UI Simplification Plan — Non-Technical Users

Goal: make SQX usable by traders with no coding/CLI experience. Hide advanced
 knobs by default, guide with plain language, and make the happy path one click.

## 1. Personas
- **Trader (non-technical)**: wants "find strategies that work" without touching
  JSON, seeds, or parameter ranges.
- **Quant (power user)**: needs full control (current UI).

Approach: two modes — **Simple** (default) and **Advanced** (toggle in settings).

## 2. Information architecture
Current 10 pages → reduce to 5 top-level tabs in Simple mode:
1. **Home** — status, one big "Start" button, recent results.
2. **Data** — import + build (already friendly; keep).
3. **Generate** — wizard (replaces Generator).
4. **Results** — strategies + plain-language metrics.
5. **Improve** — robustness/stress combined (hide unless Advanced).

Advanced mode restores all current pages.

## 3. Guided Generate wizard (3 steps)
Step 1 — **Choose data**
- Auto-select all imported instruments.
- Auto-build missing timeframes with progress bar.
- Plain text: "We'll look at H4 candles and test on every minute."

Step 2 — **Choose goal**
- Presets: Conservative / Balanced / Aggressive (sets gates & dedup).
- Sliders with words: "How many strategies do you want?" (target).
- Timeframe picker with icons (M5, H1, H4, D1) and one-line explanations.

Step 3 — **Run**
- Big "Start search" button.
- Live progress: "Tested 1,234 ideas · Found 12 good ones · ~3 min left".
- Stop anytime.

Advanced fields (indicators pool, gates, session, orders, risk params) move to
an "Advanced options" drawer, collapsed by default.

## 4. Language & metrics
- Replace jargon:
  - Sharpe → "Consistency score"
  - Return/DD → "Profit vs worst drop"
  - Stagnation → "Longest dry spell (days)"
  - OOS → "Test on unseen data"
- Tooltips with one-sentence explanations and example.
- Color badges: green/yellow/red with words, not just numbers.

## 5. Data page simplification
- One "Import all new CSVs" button (already exists) as primary.
- Show a checklist: "1. Import ✓  2. Build H4 ✓  3. Ready to generate".
- Hide quality report behind "Show details".

## 6. Results page simplification
- Card layout: strategy name, 3 big metrics, "Why it passed" bullets.
- One-click "Export to MT5" (future) / "View rules".
- Filter by "Good / OK / Weak" instead of raw numbers.

## 7. Components to add
- `Wizard`, `PresetPicker`, `ProgressRing`, `MetricCard`, `Tooltip`,
  `AdvancedDrawer`, `StatusChecklist`.
- `SimpleModeContext` to toggle UI complexity.

## 8. Defaults for Simple mode
- Indicators: all enabled (current default).
- Gates: Balanced preset.
- Autocorrelation: on, 0.95.
- OOS: 40%.
- Target: 50 (not 200) to finish faster.

## 9. Implementation phases
Phase 1 — Mode toggle + presets + wizard shell.
Phase 2 — Plain-language metrics + tooltips.
Phase 3 — Checklist + progress wording.
Phase 4 — Advanced drawer migration.
Phase 5 — User test with 3 non-technical traders; iterate.

## 10. Success criteria
- A non-technical user can go from CSV to running a search in < 3 clicks.
- No unexplained jargon on the main path.
- Advanced users can switch back to full UI instantly.
