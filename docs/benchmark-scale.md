# Benchmark: handoffs as a project grows

This benchmark measures what one handoff costs, and how much of the project the next chat still
knows, for projects of **20, 100, 500 and 2,000 facts**. It compares:

| Method | How the next chat starts |
|---|---|
| **Full transcript** | The whole previous conversation is pasted in. |
| **Summary** | The model summarizes the chat; the summary is pasted in. |
| **ctx (default)** | The model saves the changes with ctx; the next chat pulls. A small repo loads whole. A large one loads "where you left off" (constraints, goals, open questions and the latest changes, plus an index of the rest), and the model looks up anything else when it is asked about it. |
| **ctx (focus)** | As ctx (default), but the next chat pulls one area ("I'm working on the Ivory Summit service"). |
| **ctx (everything)** | As ctx (default), but the whole repo is loaded at its recommended size, whatever its size. |

Every token number is the model's own usage report, and costs are priced at Claude Sonnet 5.5
rates ($2 per million input tokens, $10 per million output and thinking tokens).

## Result in short

| | 20 facts | 100 facts | 500 facts | 2,000 facts |
|---|---|---|---|---|
| **Answers right** | | | | |
| Summary | 72% | 83% | 43% | **26%** |
| Full transcript | 100% | 100% | 100% | 100% |
| **ctx (default)** | **100%** | **100%** | **100%** | **100%** |
| ctx (focus) | 100% | 100% | 100% | 86% |
| ctx (everything) | 100% | 100% | 100% | 100% |
| **Context the next chat starts with (tokens)** | | | | |
| Summary | 383 | 558 | 514 | 495 |
| Full transcript | 3,637 | 4,380 | 11,488 | 43,876 |
| **ctx (default)** | 567 | 2,273 | **640** | **692** |
| ctx (focus) | 568 | 1,815 | 1,800 | 1,736 |
| ctx (everything) | 584 | 2,122 | 9,695 | 38,170 |
| **Cost per correct answer** (handoff, next chat, answers and 20 more messages) | | | | |
| Summary | $0.0128 | $0.0088 | $0.0187 | $0.0290 |
| Full transcript | $0.0149 | $0.0115 | $0.0208 | $0.0617 |
| **ctx (default)** | **$0.0118** | $0.0112 | **$0.0096** | **$0.0112** |
| ctx (focus) | $0.0117 | $0.0113 | $0.0113 | $0.0120 |
| ctx (everything) | $0.0116 | $0.0111 | $0.0220 | $0.0615 |

- **ctx uses more tokens than a summary, and keeps the project.** A summary stays about 500
  tokens whatever the project size, so it is the cheapest in raw tokens. It is also the only
  method that forgets: at 2,000 facts it answered a quarter of the questions. ctx (default)
  answered every question at every size, and never stated an old value as current.
- **On cost per correct answer, ctx (default) is cheaper than a summary from 500 facts on**:
  $0.0096 against $0.0187 at 500 facts and $0.0112 against $0.0290 at 2,000. At 20 facts it is
  slightly cheaper too; at 100 facts the summary is cheaper per correct answer ($0.0088 against
  $0.0112) while answering 83% instead of 100%.
- **Against the full transcript, ctx (default) is cheaper from the first message after the
  handoff** on large projects, and from a few messages on small ones (see
  [Break-even](#break-even)), with the same 100% of answers.
- **Loading everything does not scale.** At 2,000 facts, ctx (everything) carries 38,170 tokens
  into the next chat, about as much as the full transcript, and costs 5.5× as much per correct
  answer as ctx (default). That is why a large repo loads "where you left off" unless the user
  asks for everything.

## Total tokens and correct answers

**Total tokens** here is the whole measured part of the run: loading the earlier context into the
last chat, its 10 messages, the handoff, loading the next chat, and answering the questions
(lookups included). It leaves out what came from earlier chats and is the same setup for every
method (the earlier summary, and preloading the repo).

### Per project size

| Facts | Method | Total tokens | Correct answers | % right |
|---|---|---|---|---|
| **20** | Full transcript | 39,229 | 25 / 25 | 100% |
| | Summary | **40,468** | 18 / 25 | 72% |
| | ctx (everything) | 49,383 | 25 / 25 | 100% |
| | ctx (default) | 53,136 | 25 / 25 | 100% |
| | ctx (focus) | 59,362 | 25 / 25 | 100% |
| **100** | Full transcript | 56,263 | 35 / 35 | 100% |
| | Summary | **44,841** | 29 / 35 | 83% |
| | ctx (everything) | 82,210 | 35 / 35 | 100% |
| | ctx (default) | 78,515 | 35 / 35 | 100% |
| | ctx (focus) | 84,283 | 35 / 35 | 100% |
| **500** | Full transcript | 152,889 | 35 / 35 | 100% |
| | Summary | 64,614 | 15 / 35 | 43% |
| | ctx (everything) | 199,012 | 35 / 35 | 100% |
| | ctx (default) | **63,427** | 35 / 35 | **100%** |
| | ctx (focus) | 80,934 | 35 / 35 | 100% |
| **2,000** | Full transcript | 575,122 | 35 / 35 | 100% |
| | Summary | **35,200** | 9 / 35 | 26% |
| | ctx (everything) | 651,299 | 35 / 35 | 100% |
| | ctx (default) | 90,284 | 35 / 35 | **100%** |
| | ctx (focus) | 78,891 | 30 / 35 | 86% |

### All four sizes together (130 questions)

| Method | Total tokens | Correct answers | % right | Tokens per correct answer |
|---|---|---|---|---|
| Full transcript | 823,503 | 130 / 130 | 100% | 6,335 |
| Summary | **185,123** | 71 / 130 | 55% | 2,607 |
| ctx (everything) | 981,904 | 130 / 130 | 100% | 7,553 |
| **ctx (default)** | 285,362 | **130 / 130** | **100%** | **2,195** |
| ctx (focus) | 303,470 | 125 / 130 | 96% | 2,428 |

- **ctx (default) uses about 1.5× the summary's tokens** (285,362 against 185,123) **and answers
  every question** (130 against 71). Per correct answer it is the cheapest method of all: 2,195
  tokens, against 2,607 for the summary.
- **It uses about 65% fewer tokens than the full transcript** (285,362 against 823,503), with the
  same 100% of answers.
- **Loading everything** is accurate but the most expensive overall, more than the full
  transcript, because at 2,000 facts the pull is as heavy as the whole conversation. That is why
  it is only used when the user chooses it.
- **At 20 and 100 facts** every ctx method costs more than both the full transcript and the
  summary: saving and the tool definitions add a fixed amount. The advantage grows with the
  project: from 500 facts, ctx (default) is the cheapest method overall and still answers
  everything.

## The test

### The project

A generated platform project: up to 100 services, each with 20 settings (database, language,
region, message queue, cache, owner, support tier, log retention, deploy cadence, CI, monitoring,
API style, authentication, orchestrator, paging, object storage, frontend, search, rate limit,
backups). A fact reads like *"The Amber Falcon service stores its data in PostgreSQL."* A
project of N facts uses the first N service-and-setting pairs.

Before the measured part, the project already has a history: every fact was stated, and about a
fifth of them were changed once ("Change of plan: … That replaces …"). How long that earlier
conversation is:

| Facts | 20 | 100 | 500 | 2,000 |
|---|---|---|---|---|
| Earlier conversation | 425 tokens | 2,185 | 10,751 | 43,126 |

### What each method does

For every size and method:

1. **The last chat starts** with what that method carries: the earlier conversation (full
   transcript), a summary of it (summary), or a pull (ctx).
2. **The user changes 10 facts**, one message each ("Change: The Amber Falcon service caches
   with Valkey. It replaces Redis."). The model's replies are generated live.
3. **The handoff:** the summary method writes a new summary; ctx saves with `ctx_commit`
   (approving a review is done the way the in-chat view does it, with no model call); the full
   transcript has no handoff step.
4. **The next chat starts** with what was handed over. It continues with one area of the
   project, so the user says which service they are working on.
5. **The next chat is asked 35 questions** (25 at 20 facts):
   - **focus area:** every setting of that service (15);
   - **changed:** the 10 facts changed in the last chat;
   - **outside the focus:** 5 facts about other services;
   - **what came before:** 5 questions about values before the last chat's changes.

The questions end with: *"If you can't find it, say 'unknown'."* For the ctx methods, the
model can look things up with `ctx_stored` while answering, as it can in real use; those
lookups and their tokens are counted.

Each ctx method has its own last chat and its own save, on its own branch of the same
preloaded repo, so "where you left off" and "focus" also keep the last chat small.

### How ctx's default pull decides

- If everything fits in about 4,000 tokens (at the recommended size), the whole repo loads.
  Here that is the 20- and 100-fact projects.
- Otherwise it loads what always matters (constraints, goals, open questions, conflicts,
  pinned items), the 15 most recently changed items, and one line listing what else is stored,
  by area. The note tells the model to look up anything else with `ctx_stored` when the user
  asks about it.
- The user can instead pick an area, type a topic, or choose everything (at its real cost) in
  the pull view.

## Results

### Answers, by kind of question

| | Focus area | Changed | Outside the focus | What came before |
|---|---|---|---|---|
| **2,000 facts** | | | | |
| Summary | 1/15 | 8/10 | 0/5 | 0/5 |
| Full transcript | 15/15 | 10/10 | 5/5 | 5/5 |
| **ctx (default)** | **15/15** | **10/10** | **5/5** | **5/5** |
| ctx (focus) | 15/15 | 10/10 | 0/5 | 5/5 |
| ctx (everything) | 15/15 | 10/10 | 5/5 | 5/5 |
| **500 facts** | | | | |
| Summary | 2/15 | 9/10 | 0/5 | 4/5 |
| Full transcript | 15/15 | 10/10 | 5/5 | 5/5 |
| **ctx (default)** | **15/15** | **10/10** | **5/5** | **5/5** |
| ctx (focus) | 15/15 | 10/10 | 5/5 | 5/5 |
| ctx (everything) | 15/15 | 10/10 | 5/5 | 5/5 |

At 20 and 100 facts every ctx method answered everything (25/25 and 35/35); the summary
answered 18/25 and 29/35. No method stated an old value as current at any size.

At 2,000 facts, ctx (default) answered the focus-area and outside questions with 5 lookups.
ctx (focus) loaded the focus area but did not look up the 5 outside questions in this run; at
500 facts it did (2 lookups), and answered all of them.

### Tokens at 2,000 facts

| | Full transcript | Summary | ctx (default) | ctx (focus) | ctx (everything) |
|---|---|---|---|---|---|
| Context the next chat starts with | 43,876 | 495 | 692 | 1,736 | 38,170 |
| Last chat: loading the earlier context | 43,733 | 1,048 | 5,510 | 7,379 | 41,759 |
| Last chat: 10 messages | 439,320 | 24,795 | 34,634 | 47,426 | 427,990 |
| Handoff (summary or save) | 0 | 4,707 | 12,546 | 14,224 | 95,564 |
| Next chat: loading the context | 44,530 | 1,335 | 5,638 | 4,918 | 41,799 |
| Next chat: answering (incl. lookups) | 47,539 | 3,315 | 31,956 | 4,944 | 44,187 |
| **Handoff + next chat + answers** | **92,069** | **9,357** | **50,140** | **24,086** | **181,550** |
| Tool calls (pulls, save, lookups) | 0 | 0 | 8 (5 lookups) | 3 | 3 |
| Pull latency on the server | – | – | 780 ms | 780 ms | 924 ms |

The full transcript and ctx (everything) carry tens of thousands of tokens into every message
of the chat (each of the last chat's 10 messages re-read about 44,000 tokens). ctx (default)
and ctx (focus) carry a few hundred to two thousand, so ordinary messages cost about as much as
in the summary chat. Their extra cost is the save (about 12,500–14,000 tokens) and, for the
default, the lookups when the user asks about things that were not loaded.

### Tokens at every size: handoff + next chat + answers

| Facts | Full transcript | Summary | ctx (default) | ctx (focus) | ctx (everything) |
|---|---|---|---|---|---|
| 20 | 11,654 | 8,744 | 19,775 | 21,273 | 18,140 |
| 100 | 11,689 | 10,054 | 27,438 | 34,128 | 28,226 |
| 500 | 26,740 | 13,519 | 28,492 | 34,909 | 60,513 |
| 2,000 | 92,069 | 9,357 | 50,140 | 24,086 | 181,550 |

These totals cover only the handoff and the start of the next chat. Every later message adds
what the chat carries (next section), which is where the full transcript and loading
everything fall behind.

### Break-even

Counting the handoff, loading the next chat, answering, and then ordinary messages in that
chat (each re-reading what the chat carries):

| Facts | ctx (default) cheaper than the full transcript from | ctx (default) vs summary |
|---|---|---|
| 20 | message 4 | more tokens; $0.0118 vs $0.0128 per correct answer |
| 100 | message 12 | more tokens; $0.0112 vs $0.0088 per correct answer |
| 500 | message 1 | more tokens; **$0.0096 vs $0.0187** per correct answer |
| 2,000 | message 0 | more tokens; **$0.0112 vs $0.0290** per correct answer |

ctx never uses fewer raw tokens than a summary, because a summary stays about 500 tokens by
dropping most of the project. Per correct answer, ctx is cheaper from about 500 facts, and
answers every question where the summary answers a quarter to a half of them.

## When pasting the chat is cheaper

For **small projects**, moving a chat by pasting it is cheaper than ctx **at the moment of the
move**. At 20 facts, pasting the transcript cost about 5,500 tokens to load the next chat,
while ctx cost a save (about 11,000 tokens) plus a pull (about 5,000).

The advantage reverses quickly, because the pasted transcript is re-read on every message of
the new chat, while ctx's cost is mostly paid once, when saving:

| Facts | ctx (default) becomes cheaper than pasting the transcript from |
|---|---|
| 20 | message 4 of the new chat |
| 100 | message 12 |
| 500 | message 1 |
| 2,000 | the first message |

And pasting is a one-time, one-way move: it does not help when switching models, coming back
later, sharing, or moving again (each move pastes a longer chat, and a summary instead decays;
see the [handoff-chain benchmark](benchmark-handoff-chain.md#context-decay)).

**So pasting is the better choice only when all of these hold:** the chat is short, it is moved
once, the new chat will also be short, and it won't be continued or moved again. In every other
case ctx costs less over the following messages and keeps the project accurate.

ctx does not prompt users to paste instead: the server never sees the chat, so it cannot judge
its length, and teaching the chat model to give the advice would add instruction tokens to every
message of every chat. The guidance is in the README instead ("When to use ctx").

## What the results mean

**A summary is cheap because it forgets.** It stays the same size at every project size, so the
more there is to remember, the more it drops: 72% right at 20 facts, 26% at 2,000. It almost
never keeps what came before a change.

**ctx remembers at every size, and stays small by loading what matters.** Every fact is stored
on its own and stays until something changes it. A small repo loads whole. A large one loads the
essentials and the latest changes, and the rest is one lookup away. In this test that gave
100% of answers at every size, with the next chat starting from under 700 tokens at 500 and
2,000 facts.

**Loading everything is the wrong default for large projects.** It is accurate, but at 2,000
facts it carries as much as the full transcript and saving re-reads it all (95,564 tokens
per save). The pull view still offers it, with its real cost shown.

**Lookups are the part that depends on the model.** ctx (default) looked up every question
outside what it had loaded at 2,000 facts; ctx (focus) did at 100 and 500 facts but not at
2,000. A model that answers "unknown" instead of looking up loses those answers, though it does
not invent wrong ones.

## Limitations

- **One model, one run:** Gemini 3 Flash (`gemini-3-flash-preview`) on Vertex AI, standing in
  for the user's chat model. Other models summarize, save and look things up differently, and
  numbers move between runs (about ±10% on totals in earlier runs).
- **Compact, generated conversations:** each fact is one short sentence and the earlier
  conversation uses one-line scripted replies. Real chats are much wordier, which makes the full
  transcript far more expensive than here; the comparison with the summary and with ctx is not
  affected in the same way.
- **One handoff per size:** the [handoff-chain benchmark](benchmark-handoff-chain.md) measures
  four handoffs in a row on a realistic project, including how summaries decay over a chain.
- **Keyword scoring:** an answer counts as correct when it contains the expected value.
- **Question batch:** the next chat is asked all questions in one message. In real use a user
  asks a few at a time, so lookups would be spread out and each would be smaller.

## Reproducing it

Requirements: Docker running and a Google Cloud project with Vertex AI.

```powershell
uv run python -m evals.handoff_scale --project <gcp-project-id>
uv run python -m evals.handoff_scale --sizes 20,100 --project <gcp-project-id>
```

| File | What it holds |
|---|---|
| `evals/handoff_scale.py` | The generator, the methods, the questions, token accounting and the report |
| `evals/results/handoff-scale-<time>/console.txt` | The full console log |
| `evals/results/handoff-scale-<time>/results.json` | Every number, answer and score |

The numbers in this document come from `handoff-scale-20261111-161030`.
