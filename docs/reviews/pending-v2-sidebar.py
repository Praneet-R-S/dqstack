"""v2's working sidebar: eight blocks of the planner's own work.

The rule that resolves it, quoted from the report: the sidebar holds the planner's
work, not the application's features. Navigation shrinks to three destinations while
the rail as a whole gets denser, because every other block carries a live number.
`Evidence` moves to the footer -- a real destination that is not part of the daily
loop, said plainly rather than hidden.
"""
import pathlib

UI = pathlib.Path("C:/Users/prane/Documents/Product_demand_forcasting/ui")


def ic(paths, sw="1.4"):
    return ('<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" '
            f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')


I = {
    "today": '<path d="M2.5 4.5h11v9h-11z"/><path d="M2.5 7.5h11"/><path d="M5.5 2.5v3M10.5 2.5v3"/><path d="M5 10.5l1.4 1.4 2.8-2.8"/>',
    "products": '<path d="M2.5 4h5.5v8H2.5z"/><path d="M9.5 4h4v8h-4z"/><path d="M4 6.2h2.5M4 8.4h2.5"/>',
    "grid": '<path d="M2.5 3.5h11v9h-11z"/><path d="M2.5 6.5h11M2.5 9.5h11M6.5 3.5v9"/>',
    "evidence": '<path d="M2.5 13V3"/><path d="M2.5 13h11"/><path d="M5 10.5l2.5-3 2 2 3-4"/>',
    "undo": '<path d="M3 7.5h7a3 3 0 0 1 0 6H6"/><path d="M5.6 4.6 2.7 7.5l2.9 2.9"/>',
    "sun": '<circle cx="8" cy="8" r="2.8"/><path d="M8 1.6v1.4M8 13v1.4M1.6 8h1.4M13 8h1.4M3.5 3.5l1 1M11.5 11.5l1 1M12.5 3.5l-1 1M4.5 11.5l-1 1"/>',
    "rows": '<path d="M2.5 3.5h11M2.5 6.5h11M2.5 9.5h11M2.5 12.5h11"/>',
    "keys": '<path d="M2.5 4.5h11v7h-11z"/><path d="M4.6 6.7h.01M6.8 6.7h.01M9 6.7h.01M11.2 6.7h.01M4.6 9.3h6.8"/>',
}

MARK = ('<svg class="mark" viewBox="0 0 16 16" aria-hidden="true" fill="none">'
        '<path d="M5.25 1.75V14.25" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
        '<path d="M5.25 8 L14.75 2.75 V13.25 Z" fill="var(--series-forecast)" fill-opacity="0.9"/>'
        '<path d="M5.25 8 L14.75 5.4 V10.6 Z" fill="var(--series-forecast)"/>'
        '</svg>')

p = UI / "index.html"
s = p.read_text(encoding="utf-8")

start = s.index('  <aside class="sidebar" id="sidebar">')
end = s.index("  </aside>", start) + len("  </aside>")

SIDEBAR = f'''  <aside class="sidebar" id="sidebar">

    <!-- 1. Lockup, and the most urgent fact in the application beside it. -->
    <div class="sb-brand">
      {MARK}
      <span class="brand-mark">DQ&middot;STACK</span>
      <button class="sb-cycle-chip" id="sbChip" data-state="open"
              title="Where this ordering cycle stands"></button>
      <button class="sb-collapse" id="sbCollapse" aria-label="Collapse the sidebar"
              aria-expanded="true">
        <svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor"
             stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M6.5 4L3 8l3.5 4"/><path d="M12.5 4v8"/>
        </svg>
      </button>
    </div>

    <!-- 2. The cycle bar. The frame the product was missing: it converts a list of
            cards into a working week with a deadline. -->
    <div class="sb-cycle" id="sbCycle" title="">
      <div class="sb-cycle-top">
        <span id="sbCycleWeek">&mdash;</span>
        <span id="sbCycleWhen"></span>
      </div>
      <div class="sb-cycle-track"><div class="sb-cycle-bar" id="sbCycleBar"></div></div>
    </div>

    <nav class="sb-nav" aria-label="Sections">

      <!-- 3. Navigation. Three destinations. -->
      <button class="sb-item tab" role="tab" data-view="today" aria-selected="true">
        {ic(I["today"])}<span>Today</span>
        <span class="sb-badge" id="sbTodayBadge">0</span>
      </button>
      <button class="sb-item tab" role="tab" data-view="products" aria-selected="false">
        {ic(I["products"])}<span>Products</span>
      </button>
      <button class="sb-item tab" role="tab" data-view="workspace" aria-selected="false">
        {ic(I["grid"])}<span>Spreadsheet</span>
        <span class="sb-badge draft" id="sbDraftBadge" hidden>0</span>
      </button>

      <!-- 4. The queue, by cause, with exposure. The block that makes the rail an
              instrument rather than a menu: a planner reads the shape of their day
              here before reading a single word. -->
      <p class="sb-group">Your queue</p>
      <div class="sb-queue" id="sbQueue"></div>

      <!-- 5. Watching. -->
      <p class="sb-group">Watching</p>
      <div class="sb-watch" id="sbWatch"></div>

      <!-- 7. Scope. (6, saved views, is Phase 4 in the report's own order.) -->
      <p class="sb-group">Scope</p>
      <div class="sb-scope">
        <label class="sr-only" for="sbScope">Stores in scope</label>
        <select id="sbScope" class="sel"><option value="">All stores</option></select>
      </div>
    </nav>

    <!-- 8. Session: progress, exposure covered, and an always-available undo. -->
    <div class="sb-session" id="sbSession">
      <p class="sb-session-cap">Decided today</p>
      <p class="sb-session-fig">
        <b id="sbSessionN">0</b><span id="sbSessionOf">of 0</span>
        <span class="sb-session-cov" id="sbSessionCov"></span>
      </p>
      <div class="sb-session-act">
        <button class="sb-undo" id="sbUndo" disabled>
          {ic(I["undo"], "1.5")}<span>Undo last</span>
        </button>
        <button class="sb-undo" id="sbExport" disabled><span>Export</span></button>
      </div>
    </div>

    <div class="sb-foot">
      <button class="sb-health" id="sbHealth" data-state="ok">
        <span class="sb-health-dot"></span>
        <span id="sbHealthText">Checking feeds&hellip;</span>
      </button>
      <!-- Evidence sits in the footer deliberately. It is a real destination that
           analysts and examiners need, and putting it here says plainly that it is
           not part of the daily loop -- without hiding it or apologising for it. -->
      <button class="sb-item sb-small" id="sbEvidence">
        {ic(I["evidence"])}<span>Evidence</span>
        <span class="sb-dot" id="sbEngineDot" data-status="idle" aria-hidden="true"></span>
      </button>
      <button class="sb-item sb-small" data-menu="theme">
        {ic(I["sun"])}<span id="sbThemeLabel">Dark mode</span>
      </button>
      <button class="sb-item sb-small" data-menu="density">
        {ic(I["rows"])}<span>Row height: <b id="densityLabel">Default</b></span>
      </button>
      <button class="sb-item sb-small" data-menu="keys">
        {ic(I["keys"])}<span>Shortcuts</span><span class="kbd">?</span>
      </button>
      <p class="sb-engine" id="menuSqlSub">SQL console &mdash; for analysts</p>
    </div>
  </aside>'''

s = s[:start] + SIDEBAR + s[end:]

# The ribbon's job moved into the sidebar's blocks 1 and 2; the chrome keeps the
# title and the search, so the top bar stops competing with the rail.
old_ribbon = s[s.index('    <!-- Demand planning is cyclical'):s.index("    </div>", s.index('id="ribbonRight"')) + len("    </div>")]
s = s.replace(old_ribbon, '''    <!-- The cycle moved into the sidebar, where it sits above the work it frames.
         This keeps a single home for "where are we in the week" rather than two. -->''', 1)
p.write_text(s, encoding="utf-8")
print("index.html: eight-block sidebar; ribbon consolidated into it")
