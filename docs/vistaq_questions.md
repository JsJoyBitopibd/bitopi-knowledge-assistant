# Questions the assistant answers from VistaQ

VistaQ (database `VistaQ_Default_RHL`) records every QC check on the sewing lines of RHL, TAL
and BGL as it happens. The assistant answers the questions below straight from it, without a model call, in well
under a second for a day or a week (about 3 s for a whole month). Each answer shows the query and the time the rows
were read.

Every question can end with a period: **today** (the default), **yesterday**, **this week**, **last week**,
**this month**, **last month** or **last N days**. Name a factory (RHL, TAL, BGL) to see only that factory; a user
limited to some factories only ever sees those.

| Ask | You get |
|---|---|
| Show me the QC summary today · What is the DHU today? · TAL DHU this week | Per factory: pieces checked, passed, defective, rejected, defects, DHU, reject %, first-pass %, end-line output |
| Show me the defect summary today · Top defects in BGL this week | Defects found, by defect type and category, most frequent first |
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

## Not answered from VistaQ

- **Day targets and efficiency.** VistaQ holds about three production plans per line per day, so adding up their
  targets would overstate the target. Day targets come from the daily line record ("Which lines are below target?").
- **AQL and buyer final audits** are not in any system yet.
- `dbo.DefectSummary` and `dbo.QCSummary` are not used. The job that fills them runs days behind (on 7 Oct 2026 they
  ended on 29 Sep and 5 Oct). The VistaQ owners may want to look at it.

When the assistant has nothing for a question, it says "System doesn't have the data.", then one plain sentence on
what it checked or where to look, and offers questions it can answer (the same question over a wider period when
a period came back empty).
