# Questions the assistant answers from VistaQ

VistaQ (database `VistaQ_Default_RHL`) records every QC check on the sewing lines of RHL, TAL
and BGL as it happens. The assistant answers the questions below straight from it, without a model call, in well
under a second for a day or a week (about 3 s for a whole month). Each answer shows the query and the time the rows
were read.

Every question can name a period; without one it means **today**. The assistant understands:

- **relative words**: today, yesterday, this week, last week, this month, last month, last N days;
- **a date, written day first** as in Bangladesh: `6-10-2026`, `06/10/2026`, `2026-10-06`, `7th october`,
  `October 7`. A date without a year means the latest one that is not in the future;
- **a range**: `from 1 Oct to 5 Oct`, `between 28 Sep and 2 Oct`;
- **a month**: `in September`, `for October`, `September 2026`;
- **a weekday**: `monday`, `last thursday`.

A date it cannot read (`31-02-2026`) goes to model-written SQL instead of a built-in answer, so a question is never
answered for a different day than the one asked. Every table starts with a **Date** column: the day the rows cover,
or "first to last day" for a longer period. The heading names the period too ("on 6 Oct 2026").

Name a factory (RHL, TAL, BGL) to see only that factory; a user limited to some factories only ever sees those.
Asking "day wise", "daily" or "per day", or naming a QC stage, line, unit, PO, style or buyer, gets SQL written for
that question, because the built-in answers cannot apply those filters.

| Ask | You get |
|---|---|
| Show me the QC summary today · What is the DHU today? · TAL DHU this week | Per factory: pieces checked, passed, defective, rejected, defects, DHU, reject %, first-pass %, end-line output |
| Show me the defect summary today · Top defects in BGL this week | Defects found, by defect type and category, most frequent first |
| show me Unit, Line and Buyer wise DHU of TAL · defects by style and colour yesterday · line wise FTP and reject rate of RHL | Exactly the groupings and figures named, with the date (see "Grouping" below) |
| PO wise DHU of TAL yesterday · Show the DHU of EO TAL-26-1985-1 this week | QC per file, then each file's export orders and POs from the ERP (see "PO and EO" below) |
| Which export orders are in file FR-26-1985? | The file's export orders and POs from the ERP |
| Show the production dashboard yesterday · (VistaQ's dashboard card list pasted) + "show me these data of yesterday" | VistaQ's dashboard figures, one row per figure, columns BGL / RHL / TAL / All |
| Show the production dashboard yesterday line wise · What is the efficiency today? | The same figures per line; any single production figure opens the dashboard |
| Which lines have the highest DHU? · Which RHL lines had the most defects yesterday? | Each line's pieces checked, defects, rejects and DHU, highest DHU first |
| Which lines are above the DHU target today? | Lines whose DHU is above VistaQ's DHU target, with the target |
| Show the line output today · Show the TAL line output yesterday | Each line's end-line output (pieces passed at Output QC) |
| How many pieces were rejected today? · What is the reject rate of RHL this week? | Rejected pieces, pieces checked and reject rate per factory |
| Which buyers had the most defects this week? · Which styles had the most defects in TAL this month? | Defects per buyer, or per buyer and style |
| Show the QC results by stage today · stage wise QC for BGL yesterday | Checked, passed, defective, rejected and re-checked pieces at each QC stage |
| How many corrective actions were raised this week? · Show RHL CAPA last week | Corrective actions (CAPA) per factory, or the list with line, buyer, style, defect and DHU |
| What was the cutting output today? · How many pieces were cut in TAL yesterday? | Pieces cut per factory |

Other VistaQ questions (a particular PO, a particular defect on one line, an hour of the day) are answered too, with
SQL the model writes over the same views. That takes 3-8 s.

## How the figures are counted

- **Pieces checked** = first checks only: passed first time + defective + rejected. Pieces re-checked after repair are
  counted separately ("re-checked" in the stage view).
- **DHU** = defects ÷ pieces checked × 100, over all QC stages. This matches the DHU VistaQ stores on its corrective
  actions (81 defects on 2,666 pieces = 3.04).
- **End-line output** = pieces passed at Output QC.
- **Factory** = the factory the sewing line belongs to in VistaQ's workspace tree (line → unit → factory).

## Grouping: exactly the columns asked

A question that groups ("… wise", "by …", "per …", "each …", "broken down by …") gets the Date, then the
groupings it names in its order, then the figures it names, and nothing else. "Unit, Line and Buyer wise DHU of TAL"
returns Date, Unit, Line, Buyer, DHU. Factory is added only when no factory is named, because unit names repeat
across factories.

- **Groupings:** factory, unit, line, buyer, style, colour, file. PO, EO and order mean the file (below).
- **Figures:** DHU, defects, checked, FTP (rate, or "FTP qty"), defective, rectified and reject (rate or quantity),
  output. With none named: checked, defects and DHU.

Ranking questions keep their own answers: "Which TAL lines have the highest DHU?", "Which buyers had the most
defects?", "Which lines are above the DHU target?".

## PO and EO

VistaQ records QC per **file** (its FR number, e.g. FR-26-1985), style and colour. Its PO field is "TBA" on every plan
and its inspections carry no bundle or PO, so a true PO- or EO-level DHU does not exist. A file holds many export
orders and POs: FR-26-1985 has 17 and 15, FR-23-307 has 498 and 253.

VistaQ's FR number is the ERP's `FileRefID`. So a PO-, EO- or order-wise question gets QC per file [D1], with a note
saying so, followed by each file's export orders and POs from the ERP [D2]: count, first and last EO, order quantity.
An EO number (TAL-26-1985-1) leads to its file. A PO number in a question goes to model-written SQL, which cannot yet
find the PO's file in VistaQ.

## Production dashboard: how each figure is computed

| Figure | Definition |
|---|---|
| Working styles / Lines | Distinct buyer+style / lines with a plan that has working hours |
| Planned production | Σ day target of the plans (a style no longer running has 0 hours and target 0) |
| RTT | Planned × the share of the line's working sessions worked so far (= planned for a past day) |
| Sewing production | End-line output: pieces passed at Output QC, first time or after repair |
| Variance vs RTT | Sewing production − RTT |
| WIP | For the styles running in the period: cut pieces sent to the line − end-line output, since each style began |
| SAH | Σ output × the plan's SMV ÷ 60 |
| Man hours | Σ working slots (breaks excluded) × the manpower in place then (operators + helpers + ironmen, after VistaQ's manpower changes) |
| Plan man hours | Σ planned manpower × planned hours |
| Efficiency % | SAH ÷ man hours × 100 |
| Average SMV | SAH × 60 ÷ sewing production |
| FTP / Defective / Rectified / Reject % | Passed first time / defective / repaired and passed / rejected, each ÷ pieces checked × 100 |
| Overall DHU | Defects ÷ pieces checked × 100 |

The owner checked TAL on 7 Oct against VistaQ: 57 styles, 74 lines, planned 99,929, sewing production 54,838,
efficiency 49.9 %, FTP 94.44 %, DHU 6.38. Planned figures for a day can change later: VistaQ's plans for 7 Oct were
still being edited on 8 Oct (TAL planned 101,927 by the afternoon). The dashboard takes about 4–6 s, mostly for WIP,
which reads each running style's whole history.

## Not answered from VistaQ

- **AQL and buyer final audits** are not in any system yet.
- **PO- and EO-level QC**: see "PO and EO" above.
- `dbo.DefectSummary` and `dbo.QCSummary` are not used. The job that fills them runs days behind (on 7 Oct 2026 they
  ended on 29 Sep and 5 Oct). The VistaQ owners may want to look at it.

When the assistant has nothing for a question, it says "System doesn't have the data.", then one plain sentence on
what it checked or where to look, and offers questions it can answer (the same question over a wider period when
a period came back empty).
