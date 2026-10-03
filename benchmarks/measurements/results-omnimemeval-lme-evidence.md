# Which answer evidence does a reconstruct context show? Evidence metrics on OmniMemEval's LongMemEval-S

Accuracy alone cannot tell "shown the evidence and still wrong" from "never
shown it and right anyway". This instrument reads saved contexts against the
stored records and the dataset's answer labels, and says which evidence each
context quoted. No search ran and no model was called.

Sources: the published arm B run ([results-omnimemeval-lme.md](results-omnimemeval-lme.md),
v1.2, `4c01ccc`) and the points of the count curve
([results-omnimemeval-lme-count-curve.md](results-omnimemeval-lme-count-curve.md)),
whose contexts are the published ones cut at item boundaries. Script:
[`evidence_metrics.py`](../omnimemeval/evidence_metrics.py).

## How a quote is traced

The adapter stores each haystack session as one record, with a message id that
gives the session's position, so every record names its LongMemEval session.
A quote is the record's own text (passages joined by `" … "`), so each passage
is found in the question's records by exact search, in text order; the item's
head is tried first on the records stored at the item's time. A passage found
nowhere is counted as unlocated, never assigned.

The dataset's `answer_session_ids` name the answer sessions; inside them, the
turns marked `has_answer` are the evidence turns (the turns the harness lists as
`answer_evidences`).

| Metric | Per question |
| --- | --- |
| Sessions shown | answer sessions with at least one quoted character / answer sessions |
| All shown | every answer session quoted (yes / no) |
| Turns touched | evidence turns overlapping a quote / evidence turns |
| Turn characters | quoted characters inside evidence turns / evidence-turn characters |
| Evidence share | quoted characters from answer sessions / all quoted characters (pooled) |
| Per 1k tokens | answer sessions shown per 1,000 context tokens (cl100k, the judge's count) |
| Duplicate share | quoted characters that repeat an earlier quote of the same record / all quoted characters (pooled) |
| Tokens per correct | context tokens (cl100k) summed / correct answers |

Rates are averaged over questions unless marked pooled.

## Result

- **The window holds the evidence; most of its tokens are spent elsewhere.**
  With all items (twelve on 475 questions; 1,531 context tokens), the context quotes 96.2% of
  answer sessions (every one on 93.2% of questions) and touches 90.5% of
  evidence turns, but only 21.9% of what it quotes comes from answer sessions.
  Of the evidence turns' own text, 52.2% is quoted.
- **The first item carries the evidence on most questions.** An answer session
  is quoted first by item 1 on 426 of 500 questions and by items 2–10 on 67
  more; 7 questions never show one. The two items held by the block
  reservation (11 and 12) show no answer session first.
- **Fewer items lose evidence, and the share rises.** At four items (724
  context tokens) 89.6% of answer sessions are shown and 41.6% of the quoted
  text is evidence; at one item, 53.2% and 85.4%.

| Items (k) | Accuracy | Sessions shown | All shown | Turns touched | Turn characters | Evidence share | Per 1k tokens | Context tokens | Tokens per correct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 42.20% | 53.2% | 27.2% | 51.7% | 28.8% | 85.4% | 4.74 | 184.9 | 438 |
| 2 | 69.20% | 79.1% | 65.6% | 76.7% | 44.0% | 70.2% | 3.88 | 365.1 | 528 |
| 3 | 75.40% | 86.7% | 77.2% | 83.3% | 48.1% | 53.0% | 2.92 | 544.1 | 722 |
| 4 | 77.00% | 89.6% | 80.6% | 85.8% | 49.5% | 41.6% | 2.28 | 723.7 | 940 |
| 5 | 77.80% | 91.7% | 85.6% | 87.6% | 50.7% | 34.6% | 1.89 | 903.5 | 1,161 |
| 6 | 80.80% | 92.9% | 87.4% | 88.1% | 51.0% | 31.9% | 1.75 | 995.7 | 1,232 |
| 8 | 82.60% | 94.9% | 90.4% | 89.6% | 51.6% | 27.7% | 1.52 | 1,177.3 | 1,425 |
| 10 | 80.40% | 96.0% | 92.8% | 90.3% | 52.1% | 24.5% | 1.34 | 1,355.4 | 1,686 |
| full | 81.60% | 96.2% | 93.2% | 90.5% | 52.2% | 21.9% | 1.19 | 1,531.4 | 1,877 |

Accuracy is each point's own answers (the count curve re-answered every point;
full here is that re-answer, which matched the published 81.60%). Duplicate
share is 0.00% at every point: in this corpus an item holds one record, one
record is one session, and a quote's passages do not overlap, so nothing is
quoted twice. The metric is kept for stores where items share records.

Per question type, all items:

| Type | n | Accuracy | Sessions shown | All shown | Turns touched | Turn characters | Evidence share |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Knowledge update | 78 | 84.62% | 96.8% | 94.9% | 96.1% | 55.0% | 23.4% |
| Multi-session | 133 | 71.43% | 97.0% | 92.5% | 88.6% | 53.9% | 29.5% |
| SS-Assistant | 56 | 80.36% | 100.0% | 100.0% | 92.9% | 31.9% | 12.2% |
| SS-Preference | 30 | 90.00% | 96.7% | 96.7% | 82.2% | 61.6% | 11.1% |
| SS-User | 70 | 91.43% | 98.6% | 98.6% | 93.8% | 56.0% | 12.5% |
| Temporal | 133 | 83.46% | 92.3% | 86.5% | 88.5% | 53.7% | 24.9% |

The 92 wrong answers of the published run, by what the context showed:

| What the context showed | All | Knowledge update | Multi-session | SS-Assistant | SS-Preference | SS-User | Temporal |
| --- | --- | --- | --- | --- | --- | --- | --- |
| No answer session | 6 | 1 | 0 | 0 | 0 | 1 | 4 |
| An answer session, no evidence turn | 8 | 0 | 1 | 3 | 0 | 2 | 2 |
| Some evidence turns, not all | 34 | 2 | 25 | 0 | 0 | 0 | 7 |
| Every evidence turn | 40 | 6 | 11 | 8 | 3 | 3 | 9 |
| No evidence turn labelled | 4 | 3 | 1 | 0 | 0 | 0 | 0 |

At four items the same rows read 14 / 8 / 56 / 33 / 4 (of 115 wrong answers):
the questions that become wrong are mostly multi-session questions that lose
part of their evidence.

## Reading it

Exploratory; no rule was registered for these readings.

- **Reach is rarely the failure.** Six wrong answers of 92 had no answer
  session in the context. The rest had evidence in front of the reader, whole
  or in part.
- **The count curve's loss is evidence leaving.** Cutting items cuts the
  sessions a multi-session question needs, so a smaller budget that keeps
  whole items cannot keep the evidence. With all items, 78% of the quoted text
  comes from sessions the answer does not need. That is room for an allocation
  that spends the budget on evidence instead of on whole items. It is room,
  not a forecast: the labels are not available when serving.
- **Touched is not shown.** On 40 wrong answers every evidence turn was
  touched, yet a touched turn is on average only half quoted (50.2% of its
  characters on those 40, 58.5% on the 360 right answers with every turn
  touched; 3 and 26 of them quoted whole). Single-session assistant questions
  show it most: every answer session is shown, but on the 11 wrong answers
  13.1% of the evidence-turn text is quoted, against 36.5% on the 45 right
  ones. Assistant turns are long and the quote covers a small part of them.

## What it does not say

- Whether evidence reached the candidates beyond the window. Within the
  window every item quotes its record, so the window's evidence recall equals
  "sessions shown" with all items (96.2%); deeper candidates need the search
  run again with a trace.
- Whether the quoted characters of an evidence turn include the fact the
  question asks for. `has_answer` marks turns, not facts.
- Anything about other allocations; this reads the current one.

## Checks

- The 23,867 stored records match the 23,867 non-empty haystack sessions of
  the 500 questions; every evidence turn was found in its record; every quoted
  character was located (2,830,705 with all items, none unlocated).
- Three quoted parts matched more than one record; in none of them did the
  choice change whether an answer session was shown.
- Abstention questions (30) are included: all name answer sessions, 9 have
  labelled turns.
- `tests/test_omnimemeval_evidence_metrics.py` pins the tracing and the
  metrics. Thirteen mutations of the script were run against it: twelve turn
  it red; the survivor (merging spans that touch versus keeping them apart)
  gives the same sums and is equivalent.
- The definitions were written before the first run. After it, only the
  ambiguity check was added and "tokens per correct" made undefined for a
  group with no correct answer.
