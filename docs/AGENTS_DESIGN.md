# Factory-intelligence agents: design

Status: designed 2026-10-06. Phase K (Stage 1 for the Order, Finance/LC and Production agents, plus the
data-quality report) is built and released as v1.11.0; "As built" in `docs/ROADMAP.md` lists where the live
data changed the plan. Phases L–O are not started. Progress is tracked in `docs/PROGRESS.md`.

## Summary

The eight-agent "factory control tower" can be built as the next step of the Knowledge Assistant
rather than as a new system. It would be built in five phases that move from reporting, to warnings,
to recommendations, to drafts, and each stage starts only after the one before has earned trust. The
eight agents are Order, Sourcing, Production, Quality, Logistics, Finance/LC, Energy and Executive.

Four of the eight agents can start with data the assistant reads today: Order, Finance/LC, Sourcing
and Production. Logistics and Energy need new data feeds before they can do useful work.

The design rests on one rule the assistant already enforces: **software calculates, the model
explains.** Every count, date, risk level and cost comes from SQL or Python. The language model reads
those results, explains them, ranks options and answers follow-up questions with a reference on every
sentence.

The first build is Phase K: Stage-1 questions for the four agents that have data ("Which LCs expire
next month?", "Which lines are below target today?"), plus a data-quality report. The morning screen
follows in Phase L.

## 1. The control tower extends the assistant

### What the user gets

When an executive opens the assistant, a home page shows the state of the factories before anyone
asks a question:

```
Good morning, <name>                         Factory health 87 / 100  (how this is calculated)

Orders shipping in the next 30 days     ● 27 on track   ● 5 at risk   ● 2 critical

Needs your attention today
  1. Fabric not in-house, 9 days to PCD           order X     [D1]
  2. Line 3 below target three days running       TAL         [D2]
  3. LC expires in 6 days, shipment not made      LC Y        [D3]

Ask your factory anything…
```

Every line opens the cited answer behind it. The conversation then continues as it does today: "Why
is order X late?", then "What can we do about it?", then "Which option costs least?". Each answer is
cited, and every figure in it comes from a query or a calculation.

### What an "agent" means here

Each agent is a domain module: a configuration and a set of tools for one area of the business. It is
not a separate chatbot or an autonomous program. It has five parts:

| Part | What it is | Where it lives |
|---|---|---|
| Charter | The agent's operating manual: its objective, what it must never infer, how it ranks issues | `prompts/agents/<agent>.txt` (new) |
| Databases | The subset of the 17 catalogued databases the agent reads | `config/agents/<agent>.yaml` (new), pointing at `config/catalog/` |
| Tools | Parameterised SQL (fixed tools) and Python calculators: date windows, shortfall, completion forecast | `config/fixed_tools.yaml`, `src/ragbot/domain_agents/` (new) |
| Watch rules | Queries a scheduled job runs without being asked, producing signals | `config/agents/<agent>.yaml` (new) |
| Rules and playbooks | Staff knowledge written down: lead times, risk thresholds, standard responses | `config/rules/`, `config/playbooks/` (new), playbook PDFs in `data/pdfs/Playbook/` |

A question about LCs reaches the Finance/LC agent through the router the assistant already uses
(`db_router.pick_catalogs`), which picks the agent's databases and adds its charter. The Executive
agent reads no database itself. It reads the signals the other seven produce.

### What already exists

- **Tools without the model.** Fifteen fixed tools answer order questions with no model call
  (0.14 s median), from templates that pass the citation check.
- **Reach.** Catalogs for all 17 databases on the SQL Server (Phase J), read through a read-only login.
- **Scope.** Per-user factory scope, applied to every query and every search.
- **A small gateway.** Two model tiers already exist: a small model for routing and rewriting, and a
  main model for answers and SQL.
- **Scheduled jobs.** The ingestion worker and the nightly aggregates refresh show how a watcher job
  will run.

## 2. Software calculates, the model explains

The concept's own example shows the split. Asked "Will order X ship on time?", the Production agent
calls tools:

| Tool | Returns |
|---|---|
| `get_order` | Remaining 38,400 pieces |
| `get_current_output` | 7,100 pieces per day (last 5 working days) |
| `forecast_completion` | Sewing complete 14 Sep |
| `get_shipping_cutoff` | Cargo cut-off 15 Sep, QC and packing buffer 2 days |

A Python function compares the dates and returns "late by 1 day". The model writes the explanation
and the options, citing each tool result as a `[D#]` source. It never divides 38,400 by 7,100 itself.
This is non-negotiable #2 in `CLAUDE.md`, and the citation check already rejects any number that does
not appear in a source.

### How the non-negotiables hold

| Non-negotiable | How the agents keep it |
|---|---|
| 1. Every factual sentence cites a source | Briefing lines cite the signal's query result; recommendations cite the data and the playbook rule they came from |
| 2. The model never computes | Risk levels, the health score, forecasts and costs are Python or SQL. Risk is shown as a level with reasons ("HIGH: fabric 6 days late [D1]"), not a model-made percentage. Percentages come only later, from a statistical model trained on recorded outcomes (section 5) |
| 3. Databases are read-only | Watchers run guarded SELECTs through the same `rag_reader` login |
| 4. Data minimisation | The brief is a template with no model call. Follow-up answers send the usual ≤ 8 chunks and ≤ 50 rows |
| 5. The provider is swappable | The gateway stays inside `src/ragbot/llm/` |
| 6. The embedding model is fixed | Unchanged; agents add no embeddings beyond playbook PDFs, which go through normal ingestion |
| 7. Nothing is written to production | Signals, predictions, decisions, drafts and tickets go in a local SQLite store, `data/index/signals.db` (new) |

Sensitive data stays blocked. Payroll is hidden today by the sensitive-name rules and stays hidden;
the Finance/LC agent works on LCs, invoices, receivables and payables.

## 3. Data supports four agents today

Readiness from the Phase J catalog review. The table-level map is in the git-ignored
`private/AGENTS_DATA_MAP.md`, because the repo is public.

| Agent | Watches | Data today | Main gap |
|---|---|---|---|
| Order | PO status, quantities, PCD and ship dates, amendments | **Good**: curated views and 15 fixed tools | Buyer → region map ("European buyers"); quantity amendments are not tracked, only date changes |
| Finance/LC | LC opening, expiry and latest shipment dates; back-to-back LCs; receivables; payables | **Good**: LC and back-to-back LC tables carry expiry and last-shipment dates | Bank fields are sensitive; payroll stays blocked |
| Sourcing | Fabric and trims POs, promised vs actual receipt | **Partial**: PO commitment dates and goods receipts exist in two databases | No shortage table; supplier lead-time history has to be derived from those two |
| Production | Line targets and output, efficiency, WIP, manpower | **Partial**: hourly and daily line output exist | Plans and output sit in different systems; one planning system's plan tables are almost empty |
| Quality | DHU, defects, rejection, rework | **Partial**: defect and inspection tables in several systems | No AQL fields; no common defect list across systems |
| Logistics | Booking, vessel, cut-offs, ETD/ETA, documents | **Weak**: only records made after shipment (vessel, BL, on-board date) | No booking, cut-off or ETA data; needs a forwarder feed or a maintained sheet |
| Energy | kWh, gas, diesel, generator hours, kWh per garment | **Very weak**: monthly utility cost per factory | No meter data; needs smart meters or a building-management system feed |
| Executive | All of the above | Built from the other agents' signals | Depends on the others |

### Human input the agents need

Some answers cannot come from any database. Each of these is a small, owned piece of configuration:

1. **A buyer → region map** for questions like "Which European buyers ship in the next 14 days?".
2. **A written definition of "behind schedule"**, agreed with merchandising. A starting proposal: the
   ex-factory date has passed and the order has not shipped; or it is within 14 days and fabric is not
   in-house; or sewing output is under the share of the order the plan expects by today.
3. **Risk thresholds** for Stage 2, from the Level-3 interviews (section 5).
4. **Cost data** for "which option costs least": air-freight rates, overtime cost per hour, penalty terms.
5. **A logistics feed** (forwarder system or a sheet) and **meter data** for Energy.

The concept expects Stage 1 to "reveal enormous data-quality problems", and Phase J already found
some: a cancellation dated in the future, a visitor table whose date column is empty in all 64,558
rows, and plan tables with almost no rows. Phase K adds `scripts/data_quality.py` to list such issues
per domain, so owners can fix them before Stage 2 builds risk scores on top of them.

## 4. Each stage earns the next

The concept's four stages become trust gates. A stage starts for a domain only when that domain has
passed the gate before it.

| Stage | The user sees | Built in | Gate before the next stage |
|---|---|---|---|
| 1. What is happening | Cited answers to domain questions; the morning brief with counts and the attention list | Phases K, L | The data-quality report for the domain is clean enough that owners trust its counts |
| 2. What will go wrong | Green / amber / red per order on five dimensions (material, production, quality, shipping, finance), with reasons | Phase M | Several weeks of stored risk levels compared with what actually happened |
| 3. What I should do | Ranked recommended actions, each with its cost and effect computed by Python; approve or reject buttons | Phase N | Accepted recommendations show a record of good outcomes; the owner signs off on drafting |
| 4. Limited automation | Drafts: supplier escalation email, production meeting agenda, request for a new delivery date, QC investigation ticket, LC document checklist | Phase O | Drafts are shown for a person to copy and send. Nothing is sent or written to another system by the assistant |

Stage 4 changes a settled decision: the router refuses every write-style request today. Phase O adds
a `draft` route only after the owner signs off, and drafts stay in the local store.

### The morning brief (Phase L)

- **Source.** A scheduled job, `scripts/watch.py` (new), runs every agent's watch rules as guarded
  SQL and writes the results to `signals.db`. It makes no model call, so it uses none of the
  provider's daily quota.
- **Scope.** The page shows only the signals for the viewer's factories, through the same scope
  object as every query.
- **Health score.** A documented Python formula, shown under the number. Proposed (weights to be
  agreed in the interviews): over the orders shipping in the next 30 days,
  `health = 100 × (1 − (red + 0.5 × amber) / orders)`. With 34 orders, 2 red and 5 amber:
  100 × (1 − 4.5 / 34) = 87.
- **Attention list.** Ranked by consequence for shipment: days until the affected ship date first,
  then pieces affected. Ranking by order value would need FOB value, which the views leave out on
  purpose; that is an owner decision (section 6).
- **Click-through.** Every item is a question the assistant answers with references.

## 5. How the agents learn

The concept describes four levels of "training". None of them is model fine-tuning, which stays out
of this project (`CLAUDE.md`, "Decisions already made").

1. **Instructions.** One charter per agent in `prompts/agents/`, versioned like every prompt. Example
   lines for Production: never infer a quantity that is not in a factory system; use the forecasting
   tool for completion dates; rank issues by shipment consequence, not by percentage deviation.
2. **Tools.** Fixed tools and Python calculators (sections 1–2). This is where most of the value is.
3. **Staff knowledge.** Interview 10–20 experienced people: merchandising, sourcing, production, QC,
   commercial/LC, finance, logistics, engineering, factory management. Questions like "When do you
   start worrying about fabric?" and "Which suppliers' promised dates do you trust?". Each answer
   becomes data where it can be data (a supplier lead-time row: supplier A, brushed cotton,
   Oct–Nov, promised 7 days, actual 11), a rule in `config/rules/`, or a playbook PDF that the
   assistant cites like any SOP.
4. **Outcomes.** From Phase M on, every risk level, every recommendation, the decision a person made
   and what then happened are stored in `signals.db`. After some months this history can calibrate
   the rules and train a small local statistical model for delay probability. It runs in Python, and
   its percentages are calculations, not model text.

## 6. Model gateway

The concept routes cheap tasks to a cheap model and executive reasoning to a stronger one, with a
backup. Phase N extends the two functions that exist today (`get_chat`, `get_small_chat` in
`src/ragbot/llm/__init__.py`) into `get_model(task)`:

| Tier | Tasks | Model |
|---|---|---|
| small | rewrite, route, extraction | `LLM_SMALL_MODEL` (today's) |
| main | answers, SQL | `LLM_MODEL` (today's) |
| executive | cross-domain prioritisation, recommendations | a stronger model, set in `settings.yaml` |

When a call fails with a quota or timeout error, the gateway retries once on a fallback provider set
in `.env`. Every call is still logged to `calls.csv`. The concept's "private model for sensitive
data" would need a local model on the server; until one exists, sensitive domains stay blocked rather
than being sent to a provider.

## 7. Roadmap

Each phase becomes a version and ends with the usual gate: tests, `check_catalog.py --live`,
`scope_check.py` with 0 leaks, and an eval with no score down by more than 5 points. Tasks and
design notes are in `docs/ROADMAP.md`, "Phases K–O".

| Phase | Version | Delivers |
|---|---|---|
| K | v1.11.0 | Stage 1 for Order, Finance/LC, Sourcing, Production: agent registry and charters, curated views, fixed tools for the example questions, data-quality report |
| L | v1.12.0 | Watcher job, `signals.db`, the morning brief on the home page, health score; Quality's Stage-1 tools |
| M | v1.13.0 | Stage 2: risk rules from the interviews, green / amber / red per order, every evaluation stored |
| N | v1.14.0 | Stage 3: playbooks, ranked recommendations with computed costs, approve / reject and outcome records; the model gateway |
| O | v1.15.0 | Stage 4, after sign-off: drafts stored locally, never sent |

Logistics and Energy join when their data feeds exist. The interviews run alongside Phases K and L,
because Phase M needs their rules.

## 8. Decisions and risks for the owner

1. **Paid provider key (F3).** The keys in use are free tier (500 requests a day per model), and the
   PRD forbids free-tier keys with company data. The brief makes no model calls, but recommendations
   and follow-ups do.
2. **Server-side DENYs for the 15 newer databases.** They were generated before those databases were
   catalogued. Until they are regenerated, only the code hides sensitive columns there. This must be
   fixed before any unattended watcher runs.
3. **The public GitHub repo.** Agent charters and rules will describe how the business works.
   Recommended: make the repo private before Phase K. Until then, internal maps stay in `private/`.
4. **FOB value in the views.** Needed to rank the attention list by money at risk; left out on
   purpose today.
5. **Stage 4 sign-off.** Drafting re-opens the "refuse write requests" decision.
6. **Owners for the human inputs** in section 3: who maintains the buyer → region map, the
   "behind schedule" definition, the cost data and the logistics feed.
