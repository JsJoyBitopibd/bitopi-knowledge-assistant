# Changelog

All notable changes to the Bitopi Knowledge Assistant. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Each released version has a git tag (`vX.Y.Z`).

Where things stand right now (done / in progress / next) is in `docs/PROGRESS.md`. The design behind
each phase is in `docs/ROADMAP.md`.

## [Unreleased]

### Fixed
- **A database query that fails no longer searches the documents and calls the model** (Phase Q, Q5). An errored
  result was dropped before the "did a fixed tool run?" check, so a query that timed out or hit a type error was
  followed by a 10-second document search and an answer-model call that could only say "System doesn't have the
  data." — 45 seconds for the TAL dashboard question on 11 Oct, 26 for the line-2102 one. The reply now names
  what happened ("The query took longer than the time allowed. The records may still be in VistaQ; press Refresh
  data to try again."), keeps the Refresh button and the suggested questions, and costs no model call. A question
  that names no database record (a policy count, for instance) still falls back to the documents as before.
- **A number compared to a text column is quoted before the query runs** (Phase Q, Q2). Line, unit, style, item
  and company codes are stored as text even when they look like numbers, and SQL Server answers
  `Line = 2102` by converting the whole column, which fails on the first value that is not a number. Every
  curated view can now declare what its columns hold, raw tables take it from the database's own types, and a
  bare number compared to a text column is quoted before the statement is allowed to run. If the server still
  reports a conversion error, the quoting is applied once more and the statement runs again — with no extra AI
  call. Only then is the model asked, and it is now told what each column holds. The SQL retry and the
  empty-result repair share one budget of two extra calls, as agreed, instead of two separate ones.
- **The assistant reads the records a question names** (Phase Q, Q1/Q6). One parser now finds the factory, line,
  unit, style, export order, PO, file and company in a question, with no AI call: "TAL" and "tarasima" are the
  same factory, "TAL-26-1985-1" is an export order and not the TAL factory, and "line 2102", "line no. 2102",
  "L-2102" and "2102 line" are all line 2102. The built-in answers take those as parameters, the SQL prompt is
  told which column of each view holds them (and that a line number is text), a question naming only factories
  the user may not see is refused before anything is read — generated SQL used to come back silently empty, as
  it did on 11 Oct with the Factory pill on BGL — and a question with a line or unit number no longer pays for
  a routing call. A keyword tie between two agents is broken by the databases the router picked.
- The "Known values" company line is left out where no view in the shown databases has a company column: every
  TAL question used to carry "CompanyID '06' (TARASIMA APPARELS LTD.)", which means nothing to VistaQ.
- **"What style is running on TAL line 2102?" is answered from the production plan** (Phase Q, Q3), in a tenth of
  a second and with no AI call, as date, factory, line, buyer and style — the columns the question asks for.
  Every VistaQ answer used to hand any question naming a line number to the SQL model, which wrote `Line = 2102`
  against a column that holds text and failed. Four built-in answers now take the line, factory or style as a
  parameter: the style running on a line, that line's quality figures, the lines a style is running on, and every
  line's planned style for a day. The per-line production dashboard also names each line's buyer and style
  instead of only counting them, so "with style showing" is answered.
- `rag.vw_PlanDay` gains StyleName, Color, FileNo (the ERP file) and OrderQty; the VistaQ catalog gains the words
  staff use for the plan (running, production plan, day target, line plan, efficiency), notes for its raw tables,
  and the rule that Line and Unit are text.
- The `VISTAQ` database on the production server — a 30-table prototype with 138 plans and no views — is no longer
  ranked for questions (`routable: false`); the live system for all three factories is `VistaQ_Default_RHL`. It
  stays loaded, so a pasted statement against it still runs.
- **The production dashboard answers for one factory again** (Phase Q, Q4). Asking it for TAL ran the same five
  joined views with `(@factory = '' OR Factory = @factory)` in each subquery, which SQL Server planned far worse
  than the plain all-factory query: 8.1 seconds for every factory, a 30-second timeout for TAL. The statement now
  reads every factory once and the asked one is kept in Python, so the two questions share one cached result:
  measured after the change, all factories 5.2 s and TAL 0.04 s. The dashboard's figures are unchanged, and the
  card still shows the time the rows were read.
- A DISTINCT lookup run by the empty-result repair could be reported as the user's own query ("I checked
  PlanningTNA and found no matching records"); it is evidence about the data, not the question asked.

### Changed
- **The conversation is centred on every screen** (Phase Q, U3). The thread and the Sources rail are one block
  capped at 1,120 px and centred in the page, instead of spreading from the sidebar to the window edge: on a
  1,920 px screen the margins are now equal (250 px each side) and the thread keeps the designer's proportions
  (thread 776 px, rail 280 px). Below about 1,100 px the rail drops under the thread and spans its full width;
  on a phone the page keeps its 16 px gutters, the user bubble its 300 px cap, and nothing scrolls sideways.
  Stylesheet only — no change to any answer, table or button.

### Added
- **Phase P3 — questions that span two databases.** Every combination is decided in `docs/DATA_ACCESS.md` §5a:
  the 18 catalogs on the production instance, the two VistaQ catalogs on their own host. Three mechanisms:
  a fixed tool's companion from another database can now declare `merge:` and its columns are joined onto the
  main rows in Python — one table from two servers, cited `[D1][D2]` (the VistaQ QC-per-file answers now carry
  each file's buyer, export orders and POs from the ERP in the same rows); small masters are copied nightly for
  SQLite joins (`company_master`); and, **off by default** (`data.cross_database_names`), a model-written statement
  may read a sibling database on the same instance as `Database.schema.table` — the guard accepts only a raw table
  that sibling's catalog offers, never a `rag.*` view or a sensitive name, for unrestricted users only, and the
  reference card names both tables. Proven on a dev copy with the setting on; the owner decides for production.
- **Phase P2 — the recorded names, and a second look when a query finds nothing.** A nightly value index
  (`config/aggregates.yaml` specs with `entity:`; `src/ragbot/data/values.py`) copies the store, company, item
  category, store group and issue-cause names as the data records them, and matches a question's words against
  them — whole phrase, the store aliases of `entities.yaml`, then a close spelling ("mantainance store" →
  'Maintenance Store (Misami)'), scoped to what the user may see. A fixed tool's `type: value` parameter binds the
  recorded value (the stock-balance tool now filters on the store named); the SQL prompt gets a "Known values"
  line with the recorded spelling and the column to filter on; the router counts a recorded name like a keyword.
- **Self-correction, bounded** (`src/ragbot/data/empty_retry.py`). When a model-written statement returns nothing,
  the recorded values of each text column it filtered on are looked up (guarded `SELECT DISTINCT`, cited as their
  own cards); an exact hit means the zero is real; a unique close match replaces the misspelled literal and the
  statement runs again, with a note under the answer saying what was matched; otherwise at most one more model
  call shows the recorded values, or the other tables listed for the question. At most 2 extra calls per
  question, only after an empty first try, within 20 s, never on the `both` route (the owner's bounds).
- **Not-found replies after a model-written query say what was checked** in the user's own words ("I checked
  Inventory (store system) for '1178108', 'xyz store' and found no matching records"), offer the recorded values
  as questions to click, skip the documents search, and show "Refresh data".
- **Phase P1 — store questions answered right the first time.** Seven curated Inventory views: `rag.vw_StockBalance`
  (closing balance of an item per company and store — receipts minus issues in the store ledger, converted to the
  item's base unit, with category, store group and the non-production flag), `vw_StockMovement`, `vw_GoodsReceipt`,
  `vw_StockIssue`, `vw_Item`, `vw_Store`, `vw_Company`; six fixed tools (stock balance, receipts and issues, receipts,
  issues, stores holding an item, item record) that answer with no model call; an Inventory / Store agent and charter.
  The 8 Oct 2026 closing-balance question that was answered "System doesn't have the data." in 23 s now answers in
  3 s: Maintenance Store (Misami), 13 L received, 12.125 L issued, 0.875 L in hand.
- **Companies and stores by name.** `config/reference/entities.yaml` (the group's 12 companies with codes, short
  names and aliases from `SystemManager.dbo.Company`; store aliases; metric synonyms). A fixed tool's `type: entity`
  parameter turns "companyid=02" or "misami" into the bound parameter '02', never question text.
- **The router hears the question's own words.** Catalog `synonyms:` ("closing balance" → "closing stock"),
  `table_notes:` (one-line descriptions for discovered tables, seen by the prompt and the schema index) and a per-catalog
  `schema_rag_k`; compound tokens split on real column names (`companyid` → company id); SQL reserved words stripped
  from a pasted statement ("ORDER BY" no longer scores the order agent); ties broken by table-name share, then keyword
  hits, not the alphabet; a third catalog is shown when it ties the second on keywords; "please find / get / fetch"
  are list cues; a column-word signal in table selection (three shared words at least). Schema recall 16/17.
- **A pasted SQL statement runs as written** — under the same guard, scope and row cap as a model-written one,
  against the catalog that offers its tables (bare names qualified with `dbo.`, `TOP (200)` added); it is never
  rewritten or scored on its words.
- **Phase P0 — the logs explain a failed data answer.** `logs/sql.csv` gains a `database` column (19 catalogs share
  one connection line, so the statement alone did not say where it ran); new `logs/sql_gen.csv` records every SQL
  attempt: the databases the model was shown, the `DATABASE:` line it wrote, the raw reply, whether the guard or the
  database refused it, the row count and the stop reason; `logs/chat.csv` gains `databases_considered`, filled on
  a not-found too. Admins see the databases under each answer.
- **Quota gauge.** `ragbot/quota.py` counts today's provider calls in `calls.csv` against `llm.daily_budget` (500,
  the free tier); the admin caption shows "calls today n/500" and `scripts/eval.py --budget` refuses a run that
  would overspend the day (the judge alone took a third of a free day).
- Phase P design in `docs/ROADMAP.md` (P0 hygiene → P1 right the first time → P2 bounded self-correction → P3
  cross-database), with the diagnosis of the 8 Oct closing-balance failure.
- **RHL's VistaQ server** as two catalogs, `VistaQ_Default_RHL` (QC inspections and defects, daily QC and defect
  summaries, production and session plans, cutting, corrective actions) and `VistaQ_Logging_RHL` (application logs),
  on their own connection line `SQLSERVER_CONN_VISTAQ`. Discovery, schema vectors, and name hints for line, unit,
  buyer, style and defect IDs. The server holds all three factories, so neither catalog declares `factory:`.
- A catalog can declare `factory:` (one factory's data only). The router then prefers it when a question names that
  factory and keeps it out when the question names another one. No catalog uses it today.
- **Live line quality from VistaQ, answered in under a second.** Six views over the raw inspection tables
  (`rag.vw_LineDayQuality`, `vw_QCDefect`, `vw_QCStageDay`, `vw_CorrectiveAction`, `vw_CuttingDaily`, `vw_QCTarget`;
  the factory comes from the line → unit → factory tree) and 15 fixed tools for the questions QC and production staff
  ask: QC summary, DHU, defect summary, lines above the DHU target, lines by DHU or end-line output, rejects, defects
  by buyer or style, results by QC stage, corrective actions, cutting output. Each takes a period (today by default,
  yesterday, this or last week or month, last N days) and an optional factory. Measured live on 7 Oct 2026: 0.05–0.55 s
  for a day or a week, about 3 s for a month, the same for a user limited to one factory. DHU matches the value
  VistaQ stores on its corrective actions. The list is in `docs/vistaq_questions.md`. `dbo.DefectSummary` and
  `dbo.QCSummary` are left out: the job that fills them runs days behind.
- **Not-found replies say something useful.** After "System doesn't have the data." comes one plain sentence for
  staff (`agent/not_found.py`): what was checked and for which period when a query came back empty ("I checked VistaQ
  for today and found no matching records…"), else where that kind of information lives (each agent's `where:` in
  `config/agents/*.yaml`), never a view or table name. Under it, "Questions I can answer": the same question over a
  wider period when a period was empty, then example questions close to the question, none naming another factory
  or one particular record. The model's own not-found sentence follows the same rule (prompts v11).
- Quality watch rules on VistaQ: DHU today and reject rate today per factory (amber at VistaQ's own targets, 3.85
  and 1 %), replacing the DHU of the stale daily line record.
- Golden cases 70–79 (VistaQ questions, the "toady" typo, an AQL question that must be not-found).
- **Written dates in VistaQ questions** (owner's screenshots, 8 Oct): one period grammar (`tools.PERIOD`, written
  `{period}` in a tool's `match:`) adds dates written day first (`6-10-2026`, `06/10/2026`, `2026-10-06`,
  `7th october`, `October 7`), ranges (`from 1 Oct to 5 Oct`), months (`in September`, `for October`) and weekdays
  (`last thursday`). Answer headings name the period ("on 6 Oct 2026", "in October 2026"), and follow-ups keep it. A
  date that cannot be read goes to model-written SQL. Before, "DHU of 6-10-2026" was answered for today.
- A **Date** column first in every grouped VistaQ answer: the day the rows cover, or "first to last day".
- `defects_by_line_window`: "defect summary line wise / by line / broken down by line" gives defects, defective and
  checked pieces and DHU per line. It had repeated the by-type table.
- Golden cases 80–83 (the two dated DHU questions from the screenshots, line-wise defects, day-wise QC).
- **VistaQ's production dashboard.** VistaQ's dashboard cards, pasted with "show me these data of yesterday", are
  answered as one row per figure, columns BGL / RHL / TAL / All. The figures: working styles, lines, planned
  production, RTT, sewing production, variance vs RTT, WIP, efficiency, average SMV, man hours and plan man hours,
  SAH, FTP / defective / rectified / reject rate and qty, overall DHU, defects.
  - New views `rag.vw_PlanDay`, `vw_PlanOutputDay`, `vw_LineDayManHours` (working-session slots, with VistaQ's
    manpower changes) and `vw_CombinationFlowDay` (cut input and output, for WIP); `Rectified` on `vw_LineDayQuality`.
  - Tools `production_dashboard_window` and `production_by_line_window` (any production figure, or "dashboard").
  - Definitions are in `docs/vistaq_questions.md`.
  - Before, this question got "System doesn't have the data." after 33 s; now about 4–6 s.
- **Answers grouped exactly as asked** (owner's rule, 8 Oct): "Unit, Line and Buyer wise DHU of TAL" gives Date,
  Unit, Line, Buyer, DHU and nothing else.
  - Fixed tools can take `group_by` and `measures` params, read from the question and filled from a whitelist in
    the tool's YAML.
  - New view `rag.vw_PlanQualityDay` (QC by buyer, style, colour and file); tool `quality_by_group_window`.
- **PO and EO questions answered per file.** VistaQ records QC per file (FR number = the ERP's FileRefID), not per PO
  (always "TBA") or EO.
  - A PO-, EO- or order-wise question gets QC per file, with a note saying so, and each file's export orders and POs
    from the ERP as a second cited source ([D2]).
  - This uses a new `companion:` option on fixed tools: a second query on another database, with bind parameters,
    in a two-source templated answer.
  - "DHU of EO TAL-26-1985-1" leads to its file (`quality_by_eo_window`); "Which export orders are in file
    FR-26-1985?" lists them (`orders_by_file_id`).
- Golden cases 84–88 (the pasted dashboard, per line, the screenshot grouping question, PO-wise, EO).
- `scripts/set_db_connection.ps1` sets the connection lines in `.env` from a hidden password prompt. `-Variable` and
  `-Database` write one extra line for a database on another server.
- **Design system** in `docs/design/`: the owner's design set for every assistant surface (web app, Android app,
  website widget). It holds the original export (HTML + PDF), one PNG per screen, each screen's unpacked markup
  (`source/*.html`, the exact values), and `DESIGN.md` (tokens, type, components, and which screen goes with which
  surface). `scripts/unpack_design.py` unpacks a new export.

### Changed
- **Web app restyled to the desktop design** (screens 1 and 2). Dark green sidebar with New chat, Factory pills,
  Search in, document categories, Recent (this session's conversations) and a Data status card. On an empty
  conversation: the home briefing (date and status, greeting, data-age banner, KPI cards with a health ring,
  "Needs your attention" with Critical/Watch badges, "Try asking"). Once a question is asked: a header with Export
  (conversation and references as Markdown), the thread (dark question bubble, B avatar, a "Queried … · N rows"
  progress line, tables inside the answer, Copy / CSV / Refresh / thumbs, follow-up pills) and a **Sources** rail
  (one card per [D#]/[P#] reference with Show query or Open PDF, then Data freshness). IBM Plex fonts are served by
  the app (`src/ragbot/static/fonts/`, SIL OFL), with no call to Google Fonts. Code: `src/ragbot/ui/`, theme in
  `.streamlit/config.toml`.
- The **Factory** pills narrow the user's scope in code (`ui/view.narrow_scope`; never wider than the user's own
  scope), so document filters, SQL predicates, caches and the brief all follow the pick. The answer path, citation
  checks and logging are unchanged.
- Left out on purpose: **Share** (conversations are not stored) and on/off switches for **Search in** (the router
  picks the source). See `docs/design/DESIGN.md`.
- **Sortable answer tables**: every table in an answer has a sort button beside each column name (ascending →
  descending → the answer's order). Numbers and dates sort by value, codes naturally (`TAL-26-1741-2` before
  `…-10`), and blank cells always go last. A templated answer's 25-row preview is drawn with all the rows its query returned
  (up to 200), so sorting covers every row. The sorting runs in the browser (`ui/sort_table.js`), with no query and no
  model call. The Sources rail now moves under the thread when the window is too narrow for both.
- `dhu_by_line` and `dhu_by_line_by_factory` read VistaQ (live, today by default) instead of the daily line record
  that stopped on 20 Sep; golden cases 68–69 follow. The Quality agent reads VistaQ first, the Production agent
  gains VistaQ's end-line and cutting output, and both charters say how VistaQ counts (prompts v11).
- Date periods: "last week", "last month", and the typo "toady" (read as today, shown as "today").
- **Faster SQL generation**: `llm.max_tokens_sql` 1536 → 4096. Gemini spends hidden thinking tokens before it writes
  the SQL; at 1536, a third of the calls stopped at the limit and were retried, costing about 3 s each.
- **No slow first question after a restart**: the warm-up runs the embedder and reranker once and creates the model
  clients. The embedder's first run had cost the first model-written question about 12 s (its data step went from
  18.5 s to 6.5 s). The admin timings now show `data.embed` and `data.schema`.
- Fixed tools can declare `unless:`; the VistaQ tools use it so a question naming a QC stage, line, unit, PO, style
  or a buyer goes to model-written SQL that applies the filter, instead of a fixed answer that ignores it ("…at Waist
  QC yesterday" had been answered over every stage).
- **Dhaka time**: the image sets `TZ=Asia/Dhaka`. On UTC, "today" was the day before between 00:00 and 06:00 in
  Dhaka, and "as of" times and the Data status card ran 6 hours behind. Log timestamps are Dhaka time from 8 Oct
  2026 09:45; earlier rows are UTC.
- VistaQ questions that ask "day wise", "daily" or "per day" go to model-written SQL, which can group by day.
- `defects_by_line_window` is replaced by `quality_by_group_window` (golden case 82 now expects only the columns
  asked). `match_fixed_tool` collapses whitespace, so a pasted multi-line question matches. It also reads regex
  groups by the pattern's group count: `m.lastindex` hid nested groups, so `FR-(26-1985)` lost its inner group.
  Quality and Production charters describe the new views and definitions (prompts v13).
- `scripts/check_catalog.py --live` checks a view with `WHERE 1 = 0`, so it no longer totals an aggregating view's
  88 million rows on a live server.
- Wording: "Found N matching rows" instead of "N row(s) found"; the not-found note under an answer points to the
  suggested questions; the home "Try asking" list, the input placeholder and the greeting reply mention line
  quality.

### Fixed
- **The `aggregates` service was not running** (the local copies dated from 7 Oct); it is up again and now also
  refreshes the value index. The `ingest-worker`, `watch` and `aggregates` containers reported "unhealthy" because
  the image's health probe is the Streamlit port they do not serve; the probe is disabled for them.
- **A companion or lookup result added its own 50 rows to a provider call**; the row budget is now shared across
  the results of one call.
- **String parameters were bound as NVARCHAR against VARCHAR columns** (Phase P1). pyodbc sends a Python string as
  NVARCHAR; every code column in these databases is VARCHAR, and `varchar_column = nvarchar_parameter` converts the
  column side, which defeats index seeks and predicate push-down: the stock-balance view answered a literal in 1.2 s
  and the same bound parameter timed out at 45 s. ASCII string parameters are now bound as VARCHAR
  (`connectors._bind_varchar`); every parameterised fixed tool benefits.
- **A fixed tool that found no rows triggered a documents search** (6–54 s of reranking on this VM) and a
  model-written not-found instead of the plain "I checked … and found no matching records" sentence. The fallback
  now runs only for model-written SQL: a question that matched a tool's pattern is a data question by construction.
- **Decimal ledger sums rendered with ten decimals** (12.1250000000). Values with more than four decimal places are
  trimmed (12.125); the citation verifier normalises numbers the same way, so the text still verifies.
- **A model-written statement that returned nothing was the day's cached answer** (Phase P0). The question → SQL
  cache stored every statement that ran, whatever it returned, so an empty or NULL result was replayed for the rest
  of the day (8 Oct 2026: the closing-balance question failed again nine minutes later from the cache), and the
  "drop" after a failed replay was a no-op (`TTLCache.put` with a zero TTL returns early). A blank result is now kept
  only for `data.result_cache_ttl_seconds` (180 s) and a failed replay is really removed (`TTLCache.pop`).
- **A one-cell NULL/0 result cost an answer-model call before the documents were searched.** `[[None]]` is a truthy
  row list, so the orchestrator called it rows, sent it to the model (which said not-found) and only then searched
  the documents. It is now treated as no rows: one documents search, no model call on the empty rows (3 provider
  calls instead of 4 on that question).

## [1.12.0] — 2026-10-06 — Phase L: watcher and morning brief

The home screen now opens with the factory's state before anyone asks: the watcher runs the agents' watch rules every
hour (16 signals in ~36 s, no model call) and the brief shows the viewer's factories only. Eval data + both (20 cases,
2 new, `eval/results/20261006T0846.json`): 100% on every metric. Leak suite 34 cases, 0 leaks. 493 tests.

**Upgrading:** rebuild the image and start the new compose service (`docker compose up -d watch`). New settings with
defaults in the example: `brief.enabled`, `brief.max_items`, `brief.stale_after_minutes`, `ui.utc_offset_hours`.
`rag.vw_LineDailyOutput`'s `DefectPcs` is now `Defects` (plus `DefectivePcs`); `rag.vw_OrderShipment` leaves out
quantity-0 orders. Run `private/rag_reader_grants.sql` as `sa` (generated for all 17 databases).

- **Morning brief (Phase L)**: on the empty-conversation screen, a greeting in factory time, the factory health score
  (100 × (1 − (red + 0.5 × amber) ÷ checks), with its calculation), totals such as orders behind schedule and orders
  shipping in 30 days, a "Needs your attention" list whose "Ask" buttons send the item's question, a warning line for
  stale data, and when the watcher last checked. Only the viewer's factories. No model call.
- **Watcher**: `scripts/watch.py` (compose service `watch`, every 60 minutes) runs the agents' watch rules (`watch:` in
  `config/agents/*.yaml`: one guarded SELECT over rag.* views per rule, provisional thresholds in config) and keeps every
  run in `data/index/signals.db` (`src/ragbot/domain_agents/watch.py`, `signals.py`, `brief.py`).
- **Quality agent**: `config/agents/quality.yaml` + charter; tools for lines by DHU on the latest production day (all,
  by factory). `rag.vw_LineDailyOutput` gains `Defects` and `DefectivePcs` — the defect count was mislabelled as
  defective pieces.
- **Grant script** for all 17 databases rendered to `private/` (the owner runs it).
- **Fixed**: `rag.vw_OrderShipment` counted quantity-0 placeholder orders (PO "TBA…") as behind schedule; they are left
  out and counted in the data-quality report (new check, 15 in all).

## [1.11.0] — 2026-10-06 — Phase K: Stage-1 domain agents

The first stage of the factory-intelligence design (`docs/AGENTS_DESIGN.md`): the Order, Finance/LC and Production
agents answer their Stage-1 questions from fixed tools with no model call (0.1–0.9 s live), and the data-quality report
shows what the source systems need before Stage 2 (risk levels) can rely on them. The Sourcing agent exists but its
tools wait for current goods receipts. Eval data + both (18 cases, 5 new, `eval/results/20261006T0614.json`): 100% on
every metric. Leak suite 34 cases, 0 leaks. 475 tests.

**Upgrading:** rebuild the image (new `config/agents/`, `config/data_quality.yaml`, prompts v9); `paths.agents_dir`
defaults to `config/agents`. `chat.csv` gains an `agent` column (the old file is renamed on the first write). Run
`python scripts/data_quality.py` and give the report to the data owners.

- **Domain agents, Stage 1 (Phase K)**: `config/agents/` (order, finance_lc, sourcing, production) with charters in
  `prompts/agents/`; `src/ragbot/domain_agents/` picks the agent without a model call (a fixed tool's `agent:` tag, else
  the most keyword hits; a tie means none), adds its charter to the SQL and answer prompts, and makes sure the SQL model
  sees at least one of its databases. Every fixed tool names its agent; `chat.csv` gains an `agent` column; admins see
  it under the answer. `check_catalog.py` validates agents and tags.
- **New views**: `rag.vw_ExportLC` (expiry and last shipment date after the latest amendment), `rag.vw_BackToBackLC`,
  `rag.vw_OrderShipment` (invoiced for export or not — the order's ShipmentStatus is not maintained) and
  `rag.vw_LineDailyOutput` (PlanningTNA daily line record). `rag.vw_FileRef`'s fabric/trims in-house dates are now
  described as plans, which they are.
- **New fixed tools** (no model call): LCs expiring; back-to-back LCs expiring (count, list); lines below target on the
  latest production day in the data (all, by factory); confirmed orders past their ship date with no export invoice
  (count, list, by factory; default the last 30 days). Ranking and grouping questions skip them and go to model-written
  SQL. Date windows: `today`, `yesterday`, `tomorrow` (a "today" question used to query 7 days), a `default:` for an
  optional window, `@today` in tool SQL.
- **Data-quality report**: `config/data_quality.yaml` (14 read-only checks) and `scripts/data_quality.py` →
  `logs/data_quality_<date>.md`.
- **Fixed**: model-written SQL that reads no table (e.g. `SELECT 'System doesn''t have the data.'`) was templated into a
  cited one-row "database answer" since v1.10.0; it is now treated as no data (scope leak suite case s33).

## [1.10.0] — 2026-10-06 — Phase J: every database, faster data answers

All 17 non-test databases on the SQL Server are catalogued and answered through one read-only login,
`rag_reader` (no more `sa`). A model-written SQL question takes **5.98 s** p50 (`eval/latency/20261004T1036.json`;
8.47 s in the I1 baseline with 2 catalogs); the database is picked in 15–20 ms with no model call (16/16 sample
questions right). Data + both eval (13 cases, `eval/results/20261004T1033.json` + data re-run `20261004T1036`):
100% on every metric. 435 tests. The full 62-case eval was not re-run (free-tier quota); the documents path is
unchanged since v1.9.0.

**Upgrading:** in `.env`, point the SQL Server connection strings at `rag_reader` (the grant script comes from
`scripts/gen_reader_grants.py`, run by a DBA as `sa`). New settings with defaults in the example:
`data.max_databases_per_question: 2`, `data.sql_cache_ttl_seconds: 86400`, `answer.templated_generated: true`,
`auth.open_groups` (only for `auth.provider: none`). Mount `config/catalog` into the container
(`docker-compose.override.yml`) so a catalog edit needs no rebuild. Still open: re-run the grant script so the
15 newer databases' sensitive columns are denied at the server too (until then the code hides them).

- **Design for factory-intelligence agents (Phases K–O)**, docs only: `docs/AGENTS_DESIGN.md` (eight domain
  agents, four stages of trust, the morning brief, the model gateway, data readiness, owner decisions) and the
  tasks in `docs/ROADMAP.md` "Phases K–O". Nothing is built yet.
- **Read-only SQL Server login** `rag_reader` replaces `sa` (F2). `scripts/gen_reader_grants.py` writes the grant
  script (server-level `CONNECT ANY DATABASE` + `SELECT ALL USER SECURABLES` + `VIEW ANY DEFINITION`, then a `DENY`
  on every sensitive table and column schema discovery found — BitopiSplint 27 objects + 332 columns, Production
  4 + 11) and its rollback to the git-ignored `private/`; the password stays a sqlcmd variable. `docs/DATA_ACCESS.md`
  §1 explains why read on base tables (virtual views) and keeps the schema-only variant as the F2b target.
  `tests/test_gen_reader_grants.py` (5).
- **No-sign-in mode** `auth.provider: none`: the app skips the sign-in form and every visitor is one shared
  user, `open`, scoped to `auth.open_groups` from `config/scopes.yaml` (fails closed when none of them is
  configured). The default stays `ldap`. `tests/test_auth.py` (+2).
- **No Deploy button**: `.streamlit/config.toml` sets `client.toolbarMode = "viewer"`, so Streamlit's toolbar no
  longer offers "Deploy now" (publishing to Streamlit Community Cloud) or Clear cache.
- **Every database on the SQL Server is catalogued (Phase J)**: 15 new catalogs (HR, Hrms5Misami, erp, Inventory,
  PlanningTNA, Washing, FM, Dashboard, BiMob, BIMOB_MVC, BIMOB_MVC_TAL, DiskManager, HWATT, SystemManager,
  VISTAQ — 3,715 offered tables in all with BitopiSplint and Production) answered through schema RAG over the
  discovered tables; `scripts/gen_catalog.py` writes a catalog from a discovery file. One connection string per
  server: `connection_database:` in a catalog points the shared `rag_reader` connection at its database
  (`connectors.dsn`; `discover_schema.py --database`). `data/sensitive.py` also hides medical, tax, bonus, pay and
  increment names. `config/catalog` is mounted into the container (no rebuild per catalog).
- **Faster data answers (Phase J)**: `data/db_router.py` ranks the catalogs for a question (keywords, table
  names, schema vectors; ~15 ms, no model call) and the SQL model sees only the best two instead of every
  catalog; model-written SQL results are written from a template like fixed tools (no answer-model call;
  `answer.templated_generated`); a question asked again the same day replays its generated SQL
  (`data.sql_cache_ttl_seconds`); count/list questions in a database's own vocabulary skip the routing model.
  `index_schema.py --try` prints the database ranking; `get_index` picks up new vector files without a restart.

## [1.9.0] — 2026-09-29 — Phase I: speed

Document answers **7.45 s → 5.71 s** (p50, `eval/latency/20260929T1039.json` vs the I1 baseline
`20260929T0930.json`); the sources are shown at 2.05 s; the first word of an answer comes at ~5.7 s because
the provider sends each answer in one burst. While the worker ingests, answers stay as fast (5.66 s p50, n = 10;
search +12% p95, n = 33). Eval (62 cases, `eval/results/20260929T1012.json`, vs v1.8.0): hit rate 96% → 96%,
faithfulness 100% → 98%, correctness 91% → 90%, citation validity, not-found and refuse 100%; case 43 was a
429 quota error (asked again: correct), case 53's answer is byte-identical (judge variance). `hit_rate.py`
29/30. I5 (int8 ONNX embedding model for bulk loads) waits for approval.

**Upgrading:** `config/settings.yaml` → `retrieval.rerank_candidates: 8` (the new default in the example);
`RERANK_MAX_LENGTH` now defaults to 384 (remove a `RERANK_MAX_LENGTH=512` line from `.env` to use it).

### Added
- I1: every answer carries a request id and per-stage timings (`src/ragbot/trace.py`). The id is written
  to `chat.csv`, `calls.csv` and `sql.csv`, so one question can be followed through all three.
  `chat.csv` also records the seconds per stage, and administrators see them under each answer.
  `scripts/latency.py` prints p50/p95 per stage, end to end and to the first token, over 51 fixed
  questions, each run cold.

- I3: while an answer is being written, the progress line names its sources ("Reading: Bitopi_IT_SOP_Manual_v2
  p. 12; …") as soon as the search is done, at 2.1 s (p50). The provider sends each answer in one burst, so
  the first word cannot come sooner than about 5.7 s.

### Changed
- I2: 8 rerank candidates instead of 10, and a reranker window of 384 tokens instead of 512 (hit rate
  unchanged, 29/30): reranking 3.2 s → 1.9 s. Document answers 7.45 s → 5.71 s (p50).
- I4: the ingest scripts give the CPU to the chat app: below-normal priority, 4 embedding threads and a
  pause while the app is searching (`ingest.priority`, `ingest.embed_threads`, `ingest.yield_to_app` in
  `config/settings.yaml`). While the worker ingested, the app's document search slowed by 131% (p95);
  now by 12%.
- I2: the question embedding went from 0.46 s to 0.09 s (p50). onnxruntime no longer keeps its threads
  spinning after a rerank, and torch no longer uses every logical CPU. Ingestion is 54% faster for the
  same reason: 16 threads were slower than 8.
- I1: `chat.csv`, `calls.csv` and `sql.csv` gained columns (`request_id`; `chat.csv` also `seconds` and
  `timings`). The first write after the upgrade renames each old file to `<name>.<timestamp>.csv`, as for
  earlier header changes.

### Fixed
- I2: Windows power throttling (EcoQoS) ran the models 6–9× slower in processes it treats as background —
  the app as a service, the worker, the scripts. Each of them now opts out when it loads the models.

## [1.8.0] — 2026-09-29 — Phase H: ingestion robustness

Eval (62 cases, `eval/results/20260929T0919.json`, vs v1.7.0 on the same cases): hit rate 98% → 96%,
correctness 92% → 91%, faithfulness, citation validity, not-found and refuse 100%. The hit-rate dip is
case 28, whose answer call got the provider's per-minute quota error (429); asked again on this code it
gives v1.7.0's answer and cites the expected page. Case 2 lost a second sentence (run-to-run variance,
as in v1.3.0); case 37 went back to 1.0. Leak suite: all 34 cases, including the 7 that need the model,
0 leaks (`eval/scope/`). H4 (the scheduled tasks) is the operator's step; the README has the commands.

### Added
- H2: `scripts/inspect.py --check` compares the registry, the keyword index and the vector store (orphans
  each way, superseded flag and access tags that differ from the document's, half-written documents)
  and exits 1 on any mismatch; `scripts/ingest.py --redo <file>` rewrites a document from the embedding
  cache to repair it.
- H3: failed files are retried on the next two passes, then left until they change
  (`inspect.py --failed` shows the attempts and whether the previous version is still served). The
  sidebar shows when the folder was last checked, and administrators see files that failed or were
  skipped.
- H4: README runbook for running the worker and the aggregates refresh as scheduled tasks.

### Changed
- H3: a pass that finds nothing new takes seconds. Unchanged files (same path, size and modification
  time) are not read, and the embedding model loads only when something needs embedding.
  `ingest_state.json`, whose change makes the app drop its caches, is rewritten only when the index
  changed; every pass writes `data/index/last_scan.json` instead.
- H3: `scripts/ingest.py` and `scripts/reindex.py` take the worker's index lock (exit 3 while another
  ingest writes). The holder refreshes the lock every minute, so a long bulk ingest is never taken over,
  and a lock whose process died is taken over at once.

**Upgrading:** run `python scripts/ingest.py` (or let the worker pass) once. It records where each indexed
file lives, which the download button now needs. Nothing is re-embedded.

### Fixed
- H1: two PDFs with the same file name in different folders overwrote each other in the index. The copy
  already indexed is kept and the other is skipped and reported until one is renamed. The PDF download
  button found files by name and could serve the skipped copy from a folder the user may not see. It now
  reads the exact path that was indexed.
- H2: a failed update of a document deleted its vectors first, leaving the old version half-searchable.
  The new version is now parsed and embedded in full before the old one is replaced. A pass killed while
  writing a document is redone by the next pass (verified with hard kills).
- H3: a PDF that failed to open stayed locked by the ingest process (the logged error held pymupdf's
  file handle), so on Windows nobody could replace or delete it. A failed file was never retried until
  it changed. A moved file kept its old category. The sidebar's category list, status line and PDF
  bytes ignored the index version and refreshed only every 5–10 minutes.

## [1.7.0] — 2026-09-29 — Phase G: polish

Eval (62 cases, `eval/results/20260929T0040.json`, vs v1.6.0 on the same cases): hit rate 98% → 98%,
faithfulness 100%, correctness 93% → 92% (case 37: 1.0 → 0.5, a case that has swung between runs before),
citation validity, not-found and refuse 100%. Leak suite on this code: the 27 cases that need no model call,
0 leaks (`eval/scope/scope_20260929T0042.json`). G2's live sampling run (about 122 read-only queries) is
still to do, with the user's go-ahead.

### Added
- G3: follow-up question chips under fixed-tool answers (`follow_ups:` in `config/fixed_tools.yaml`,
  filled with the answer's parameters; each reaches a fixed tool, so a click needs no model call). Every
  fixed tool has an `example:` question: `check_catalog.py` checks it reaches that tool and `--live`
  runs all of them (it ran none before).
- G1: catalog `hints:` for code columns in raw tables — a `Buyer` code such as `C/09/7` points the SQL
  model at `dbo.Contact_Master.ContactName` (listed per table: the same column name means different
  things elsewhere).
- G2: `discover_schema.py --samples --tables-from-logs` samples only the raw tables the SQL model has
  needed (logged in `logs/schema_select.csv`; about 122 queries instead of ~6,700); earlier samples are
  kept when discovery runs again.

### Fixed
- G4: Streamlit's file watcher logged ~50 torchvision tracebacks at every start; it is off in
  `.streamlit/config.toml` (also in the Docker image). `st.dataframe` no longer uses the deprecated
  `use_container_width`.

## [1.6.0] — 2026-09-28 — Phase F: sign-in and per-user scope

Eval (62 cases, `eval/results/20260928T1820.json`, as an unrestricted user, vs v1.5.0 on the same cases):
correctness 90% → **93%**, hit rate 96% → **98%**, faithfulness 100%, citation validity 100%, not-found
100%, refuse 100%. Leak suite (`scripts/scope_check.py`): 34 adversarial cases, **0 leaks**. F2 (switch to
`rag_reader`) waits for the DBA; F3 (Gemini billing) for the user.

**Upgrading an existing installation:** copy `config/scopes.example.yaml` to `config/scopes.yaml` with the
real AD groups, set `auth.provider` and the `LDAP_*` values, then run `python scripts/ingest.py` once to tag
the documents already indexed (nothing is re-embedded). Until then, factory-limited users see no documents.

### Added
- **F1: sign-in and per-user scope** (PRD FR-4.1, 4.3–4.8, 6.2). Users sign in with their Active
  Directory account (LDAPS/StartTLS; `auth.provider: local` with bcrypt users for development); AD groups
  map to a scope in `config/scopes.yaml` (factories, departments, highest confidentiality; union over
  groups; not in a group = cannot sign in). Documents carry factory / department / confidentiality /
  buyer tags (first folder name, or a folder's `meta.yaml`), stored in the registry, the vector store and
  the keyword index; changing a tag retags in place without re-embedding. The scope is a required
  argument of every search and query: it filters the vector query and the keyword search, every SQL view
  is emitted filtered to the user's factories inside itself and the final statement is re-checked before
  it runs, raw tables and unscoped views are unavailable to factory-limited users, a fixed tool naming
  another factory is refused at once, the nightly copy is read through the same filter, and both caches
  are keyed on the scope. Signing in or out clears the conversation; admin details are admin-only; admins
  get an access and audit panel. `chat.csv` and `sql.csv` record every request's scope.
  `scripts/scope_check.py` + `tests/scope_cases.jsonl`: 34 adversarial cases, 0 leaks.
- F4: every catalog view declares `scope_column:` (its factory column); the five views without one
  (`vw_ExportOrderColorSize`, `vw_PCDChangeHistory`, `vw_CancelledExportOrder`,
  `vw_PPMMeetingReschedule`, `vw_PPMDepartmentChecklist`) gained a `Factory` / `FactoryID` column
  through joins. `scripts/gen_rag_views.py` generates the DBA's views with the per-user row filter on
  `SESSION_CONTEXT(N'rag_factories')` (`src/ragbot/data/scope_sql.py`); `check_catalog.py` flags a view
  without a scope column. The DBA request and the `rag_reader` permission scripts are kept in the
  git-ignored `private/` folder, because the repository is public.

### Fixed
- **The SQL guard accepted a name used before its CTE was defined.** In `WITH a AS (SELECT … FROM
  ExportOrder), ExportOrder AS (…) SELECT … FROM a`, SQL Server binds `ExportOrder` inside `a` to the real
  `dbo.ExportOrder` (a CTE can only use CTEs declared before it), so generated SQL could read any base
  table past the view allow-list, the raw-table rules and the user's scope. A bare name now counts as a
  CTE only where SQL binds it to one, a WITH inside a subquery is refused, and the scope check re-verifies
  every table of the final statement. Found by the F1 review; present since the guard was written.
- A retag or superseded flag that failed half-way (registry updated, vector store not) was never retried,
  leaving the vector store with the old, broader tags; the store is now updated first.
- `logs/chat.csv` had 10 header columns and 11 per row; every log now goes through one writer that renames
  a file with an older header aside.
- `scripts/inspect.py --search` would have failed now that a scope is required.
- `eval.py` compared a run with whatever file sorted last in `eval/results/` (the gate run crashed on a
  leak-suite report); it now compares only with earlier eval runs, `--compare <file>` re-compares a saved
  run, and a judge call the provider cannot answer leaves the case unscored instead of losing the run.
  Leak-suite reports moved to `eval/scope/`.

## [1.5.1] — 2026-09-28 — Hotfix H0: cautious deletes

### Fixed
- **An empty or offline PDF folder emptied the index.** Every ingest pass deleted each indexed file
  it did not see, and a missing folder simply yields no files, so one pass over an unmounted share
  removed every document — and the worker ran that pass by itself as soon as the folder changed. Now
  (PRD FR-2.14): a missing or empty folder changes nothing; a file leaves the index only when two
  consecutive passes miss it; if more than 5 % of the indexed files (and more than 3) vanish in one
  pass, nothing is removed. Each case logs an `ERROR`, puts an `alert` in `ingest_state.json`, shows
  "⚠ Index not updated" in the app's sidebar, and makes `ingest.py` / `ingest_worker.py --once`
  exit with code 2.
- `scripts/reindex.py` deleted the index before looking at the PDF folder; it now refuses (exit 2)
  when the folder has no PDFs.
- One unreadable PDF (locked by another program, no permission) aborted the whole ingest run and
  stopped the worker. It is now a failed file and the run continues; an already indexed file that
  cannot be read keeps its indexed version.
- The ingest summary's `failed` showed the registry-wide total; it is now this run's count
  (`failed_total` holds the total).
- `logs/ingest.log` was written in the Windows code page, so a Bangla file name could not be logged;
  it is UTF-8 now.
- The worker absorbed a folder change that arrived while the index was locked by a manual ingest; that
  pass is now retried at the next interval.

### Added
- `python scripts/ingest.py --confirm-removals` for a deliberate bulk removal; settings
  `ingest.removal_alert_fraction` (0.05) and `ingest.removal_alert_min_files` (3); registry column
  `document.missed_scans` (added automatically to existing registries). README "Remove documents".

### Docs
- Plan for Phases F–I (after v1.5.0) in `docs/PROGRESS.md` and `docs/ROADMAP.md`, merged from two
  sessions' proposals: hotfix H0 (an empty or offline PDF folder must not empty the index), F ready
  for more users (login + per-user scoping, `rag_reader`, billing, DBA request), G polish, H ingestion
  robustness, I speed. The open follow-ups and open items from Phases A–E are now tasks in it.

## [1.5.0] — 2026-09-27 — Phase D: 60K pages

Eval (62 cases, `RERANK_BACKEND=onnx`, `eval/results/20260927T1710.json`, vs v1.3.0 on the same cases):
correctness 89% → **90%**, faithfulness 98% → **100%**, citation validity 100%, not-found 100%, refuse
100%, hit rate 98% → 96% (the one miss was a Gemini rate-limit; it passes on re-run). Document answers
in the app: ~10 s (130–160 s before v1.1.0).

### Added
- `RERANK_BACKEND=onnx` is now set in `.env.example` (export the model first); `docker-compose.yml`
  mounts `data/models`; README "Faster reranking" runbook.

### Changed
- D1: keyword search moved from a pickled in-memory `rank_bm25` index (rebuilt in full after every
  ingest) to the `chunk_fts` SQLite FTS5 table in `registry.db`, updated per document by the registry.
  Category and superseded filters are applied inside the keyword search. English stopwords are left
  out of the query. At 60K chunks: no 22.9 s rebuild, no 691 MB in memory, 291 → 17 ms per query.
  Existing indexes are seeded once from the vector store on first use; `bm25.pkl` is no longer read.
- D2: category filters are pushed into the vector query (`$in`) instead of being applied after it,
  so every one of the top-k vector hits is usable. HNSW settings were measured at 60K vectors and
  left at Chroma's defaults (recall@20 0.999; the roadmap's larger graph gained nothing).
- D3/D4: the embedder and the reranker run in small batches on CPU (`EMBED_BATCH_SIZE`,
  `RERANK_BATCH_SIZE`; default 1 on CPU, 32 on GPU). Padding every text to the longest in a batch
  made batch 32 about half as fast. Cold ingest 243 s → 127 s for the 60-page manual; reranking 10
  candidates 18.7 s → 10.1 s; document answers 18–32 s → 14–28 s. Results unchanged (scores within
  1.3e-7, same order).
- D4: int8 ONNX reranker (`RERANK_BACKEND=onnx`, `OnnxReranker`), exported by
  `scripts/export_reranker_onnx.py` to `data/models/` (git-ignored). 2.7× faster than the PyTorch
  reranker (12.5 s → 4.6 s for 10 candidates), same top result on 6/6 questions, hit rate 29/30.
  New dependency: `onnx` (export only). `optimum` is deliberately not used: its current release pins
  transformers < 4.58.

### Fixed
- `embed.py` read `EMBED_MODEL`, `RERANK_MODEL` and `RERANK_BACKEND` without loading `.env`, so a
  script importing it directly ignored `.env` (the app worked only because it imported `config` first).
- Re-ingesting a file that was already superseded made it visible again: its fresh chunks were
  written as not superseded and never re-marked, because the document row had not changed.

## [1.4.0] — 2026-09-27 — Phase E3–E4: cheaper re-ingest

Ingestion-only release (the answer path is unchanged): 261 tests; retrieval `hit_rate.py` 29/30
(reranker on), same as v1.1.0–v1.3.0.

### Added
- E3: embedding cache in `registry.db` (`embedding_cache`, keyed by SHA-256 of the chunk text + model):
  a re-ingested document embeds only chunks whose text changed. 60-page manual: cold 243 s, one-page
  revision 6.9 s (1 embedded / 97 cached). `logs/ingest.log` and `ingest_state.json` report
  `embedded` / `cached` / `cache_pruned`; unused cache rows are pruned after updates and removals.
  `chunk.text_hash` column (older registries are migrated on open). An index built before E3 seeds
  the cache from the vectors already in the store on its first ingest — nothing is re-embedded
  (live index: 250 of 250 chunks seeded).
- E4: README "Revising a document": replace in place vs. add `…_v3.pdf` beside `…_v2.pdf`
  (older revision superseded and hidden from answers), and how the running app picks either up.

### Changed
- `ingest_folder()` accepts a store, registry and index folder (default: the app's), and
  `rebuild_keyword_index()` an index folder, so an ingest can run against a throwaway index.

## [1.3.0] — 2026-09-27 — Phase C: large database, schema RAG

Eval (62 cases, reranker on, `eval/results/20260927T1347.json`, vs v1.2.0 on the same cases):
correctness 85% → **89%**, hit rate 91% → **98%**, faithfulness 100% → 98%, citation validity 100%,
not-found 100%, refuse 100%. Data + both cases 9/13 → 13/13. Known cost: four document answers
are terser (cases 6, 22, 33, 37; see `docs/tuning_log.md`).

### Added
- C1: `scripts/discover_schema.py --json` writes `config/catalog/discovered/<db>.json` (git-ignored):
  every table and view with columns and types, primary and foreign keys, row counts and
  `MS_Description` text, from `sys.*` only. Optional `--samples` reads up to 30 distinct values of
  short text columns in small tables, never from a sensitive column. `data/sensitive.py`: one
  word-based definition of sensitive names (`EmpNID`, `DOB`, `BasicSalary` yes; `ManID` no).
- C2: catalogs have two tiers: the curated views (always in the prompt) and the discovered tables
  (`Catalog.tables`, rendered only when selected via `render_selected()`, sensitive columns never
  shown, at most 40 columns each). `exclude_tables:` patterns in the catalog YAML drop history
  copies, spreadsheet imports and scratch tables; sensitive and empty tables are never offered.
- C3: schema index (`data/schema_index.py`): picks the few discovered tables a question needs by
  merging vector search (bge-m3, precomputed by `scripts/index_schema.py` into
  `data/index/schema/<db>.npz`), BM25 over table documents, and a table-name match, then adds
  foreign-key neighbours and likely joins (shared key names). Works keyword-only when the vectors
  are missing or stale. `index_schema.py --check` measures recall on `tests/schema_cases.jsonl`:
  10/11 at k=6.
- C4: the SQL guard can allow raw discovered tables (`allowed_tables`, the catalog's full offered
  set) while forbidding `SELECT *` on them and requiring a `WHERE` on tables over
  `data.big_table_rows` (1M). Always on, for every query: no column or table with a sensitive name,
  and no `FOR XML/JSON`, `@@` globals, `SUSER_*`, `SYSTEM_USER`, `HOST_NAME`, `ORIGINAL_LOGIN`.
- C5: generated SQL can use raw tables. Per question, the selected discovered tables and likely joins
  go in a `{tables}` section after the static catalogs (stable prompt prefix), and the guard checks
  against each catalog's full offered set. Settings `data.schema_rag`, `data.schema_rag_k`,
  `data.big_table_rows`. Answers questions no curated view covers, e.g. supplier counts.
- C7 (part): fixed-tool results are answered from templates with no model call
  (`agent/templated.py`; `answer:` / `answer_list:` in `config/fixed_tools.yaml`): a count as one
  sentence, rows as a Markdown table, still checked by `verify()`. A date window keeps its phrase
  ("next week") as a parameter. Data + both eval cases: 9/13 → 13/13 correct.
- C7: six fixed tools mined from real questions in `logs/chat.csv` — upcoming PCDs (all, per buyer,
  per factory), the buyer list, cancelled orders in a window (count, list). Answered in 0.1–0.5 s
  with no model call. `config/clarify.yaml`: when a data question names no order ("status of the
  order") or no factory ("how many PPM meetings this week"), the assistant asks which one instead of
  guessing (route `clarify`).
- C8: database answers show their rows inside the `[D#]` reference card: a sortable table, a bar
  chart when the result is a label → number list (2–25 rows), and a Download CSV button (UTF-8 with
  BOM, so Excel shows Bangla correctly). `Answer.results` carries the rows; `data/present.py`
  decides table / chart / file name.
- C6: pre-computed aggregates. `config/aggregates.yaml` + `scripts/refresh_aggregates.py` copy
  results too slow to compute per question into `data/index/aggregates.db` (one read-only streaming
  scan, `guard.assert_read_only`, atomic swap). `pcd_history_by_eo` now answers from the copy:
  10 s with timeouts → 28–134 ms; "Refresh data" or a missing copy runs it live with the 30 s heavy
  timeout. `docker-compose.yml` gains an `aggregates` service (every 24 h); README runbook.
- Each `[D#]` source now shows the query's filter (factory, dates, order id) to the answering model
  and in the judge's view; `verify()` also accepts the row count and parameter values.

### Changed
- Prompts v5–v6 (`prompts/CHANGELOG.md`): SQL values as literals (only six date parameters exist);
  the router treats all ERP records as data; "pcs" only for garment quantities; multi-part answers
  answer the covered parts instead of replying not-found.

### Fixed
- `eval.py` compared a `--kind` subset run with the previous run's totals and printed false BLOCKs;
  it now compares with the most recent earlier run covering the same cases, scored on those cases.
- Aggregate results showed "Row key: ; ;" (the view's key columns are not in a GROUP BY result).
- Two chat turns citing the same PDF chunk created duplicate download-button keys (a Streamlit error).
- A short, self-contained follow-up ("What is the next PCD?") was sent through the rewrite model call;
  one such call took 27.6 s. Skipped now when a fixed tool already matches the question.
- `eo_by_po` matched "IT **po**licy" as PO number 'licy', hijacking any question that mentioned a
  policy and an order (eval case 60). "PO" must now be a whole word and the value contain a digit.
- `both` questions with an order code and a password/IT-policy noun were routed to `data` only
  (eval case 61).
- The catalog rule "col >= @from AND col < @to" taught generated SQL parameters that do not exist
  (only six date names are resolved), so such queries always failed.
- The curated "orders per factory shipping this month" few-shot example had `GROUP BY … ORDER BY`
  without `TOP`, which the guard always rejects, so any generated query imitating it failed once
  and cost an extra LLM call. It now has `TOP (50)`, and a test keeps every example guard-clean.
- `discover_schema.py` read the connection string from `os.environ` before `.env` was loaded.

## [1.2.0] — 2026-09-27 — Phase B: streaming, parallelism, caching

Eval (62 cases, reranker on, `eval/results/20260927T1214.json`): correctness 85% (unchanged),
faithfulness 100%, citation validity 100%, not-found 100%, refuse 100%, hit rate 93% → 91%.
Document answers 130–160 s before v1.1.0 → 18–32 s; repeat questions 0.1 s; pooled SQL 266 ms → 4 ms.

### Added
- B1: `ChatModel.stream()` yields text deltas then a final `ChatReply`; the OpenAI-compatible
  adapter streams with usage included; non-streaming adapters fall back to one delta. One
  `calls.csv` row per stream.
- B2: `orchestrator.answer_stream()` yields `Stage` / `Token` / `Replace` / `Final` events
  (`agent/events.py`). The draft is provisional: a draft that fails citation verification is voided
  with `Replace` (logged to `verify_failures.csv`) and only the verified `Final` enters history and
  `chat.csv`. `answer()` drains the stream, so `eval.py` and `ask.py` are unchanged.
- B3: the chat UI shows each stage live ("Searching documents…", "Writing the answer…"), types
  the draft as it streams, then shows the verified answer with "Answered in N s". References and
  thumbs appear immediately (no extra page rerun); suggestion chips clear when a question is asked.
- B4: keyword search runs alongside embedding + vector search; `both` questions search documents
  while the database query (and its SQL-generation call) runs.
- B5: caches. Query embeddings (LRU 512). Verified documents answers (`answer.cache_ttl_seconds`,
  default 1 h; key = normalized question + filter + index version; follow-ups and data answers never
  cached): a repeat question went 32 s → 0.1 s. SQL results (`data/cache.py`,
  `data.result_cache_ttl_seconds`, default 180 s; keyed on the guarded statement + parameters; hits
  keep the original `as_of`, logged as `cache:<tool>`). **↻ Refresh data** button under database
  answers re-runs the query live.
- B6: SQL Server connection pool (`data/connectors.py`): session settings once per connection,
  rollback on every release, a failed connection is never reused, a dead pooled session is retried
  once; `fetchmany(max_rows)`; fixed tools marked `heavy: true` get `data.timeout_seconds_heavy`
  (30 s). Pooled query 266 ms → 4 ms.

### Fixed
- When a database answer came back not-found and was retried against the documents, the voided
  draft stayed on screen during the document search; it is now cleared first.
- A failed LLM call raised a pydantic `ValidationError` instead of `LLMError`, so the friendly
  quota / timeout message from v1.1.0 never appeared (`ChatReply.text` now defaults to "").
- `.env.example`: `thinking_budget: 0` is rejected by gemini-3.5-flash-lite (HTTP 400), so it is now
  128; values unquoted and comments moved to their own lines (Docker `--env-file` keeps both);
  model names updated to gemini-3.5-flash-lite.

## [1.1.0] — 2026-09-23 — Phase A: speed and UX quick wins

Commit `48e2595`. Eval (62 cases, `eval/results/20260923T1743.json`): correctness 83% → **85%** (target
met), faithfulness 100%, hit rate 93%, citation validity 100%, not-found 100%, refuse 100%.

### Changed
- Reranker: 10 candidates at `max_length` 512 (was 20 at 1024), single batch with all CPU threads.
  Retrieval hit rate 29/30.
- Answer thinking budget 1024 → 0 (extractive prompt; faster, no quality loss in eval).
- LLM client: retries 8 → 2, timeout 90 s → 30 s, so a quota failure surfaces in seconds.

### Added
- Models, index and catalogs load once at app start (`st.cache_resource`); `get_store()` and the
  catalog / fixed-tool loaders are cached (catalogs keyed on file mtime).
- Router answers unambiguous questions from regex with no LLM call; a fixed-tool match routes to
  `data` directly; rewrite is skipped for self-contained follow-ups.
- `LLMError` (quota / timeout / auth / other) mapped to friendly UI messages (`data/errors.py`);
  last-resort error guard in the UI logs tracebacks to `logs/errors.log`.
- UI: suggested-question chips, Clear chat, Admin details toggle, PDF download buttons.
- Self-updating index: `scripts/ingest_worker.py` + `index_version.refresh_if_changed()` pick up new
  PDFs without restarting the app; `docker-compose.yml` runs app + worker.

### Fixed
- Count-phrased policy questions ("how many licences does the Group have?") no longer misroute to
  the database.

## [1.0.0] — 2026-09-22 — Pilot (milestones M0–M6)

Commit `27ecd09`. RAG over IT SOP PDFs (250 chunks) and live SQL Server with a citation on every fact;
Streamlit UI; Docker image `bitopi-assistant:1.0`; 99 unit tests. Eval: correctness 83%.
