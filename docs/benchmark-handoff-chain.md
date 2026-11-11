# Benchmark: continuing a project across chats

This benchmark measures what happens when a project is planned over several chats and carried
from one chat to the next. It compares three ways of handing a project over:

| Method | How a new chat starts |
|---|---|
| **Summary** | At the end of a chat, the model is asked to summarize it. The summary is pasted into the next chat. |
| **ctx** | At the end of a chat, the model commits the project state to a ctx repo. The next chat pulls it. |
| **Full transcript** | The next chat receives the entire previous conversation. This is the reference: the most context, at the highest cost. |

For each method it measures three things: **tokens** (exact, as reported by the model's API),
**how much of the project the new chat still knows**, and **whether old values come back as
if they were current**.

## Result in short

After four handoffs:

| | Summary | ctx | Full transcript |
|---|---|---|---|
| Answers right | **50%** | **86%** | 91% |
| Facts from chat 1 still known after 4 handoffs | 4/8 | **8/8** | 8/8 |
| Old values stated as current | 0 | 0 | 0 |
| Context the last chat started with | 433 tokens | 768 tokens | 12,853 tokens |
| Total tokens for the whole project | 107,403 | 154,781 | 288,525 |

- A pasted summary is the cheapest, but by the second handoff it has dropped core facts
  (team size, budget, the product approach) and it never keeps what came before a change.
  After four handoffs, it answers only half the questions correctly. Facts from the first chat
  fade with every handoff, even when nothing changed them (see [Context decay](#context-decay)).
- ctx keeps nearly everything (86%, against 91% for the full transcript), loses none of the
  first chat's facts and keeps the full history of changes, while starting each chat with a
  context about one-seventeenth the size of the full transcript. It uses about 1.4× the
  tokens of the summary method and 46% fewer than the full transcript.

## The test

### The project

A fictional product, **ClinicSlot**: appointment booking for small clinics in Pune. It is
planned over **5 chats** with **30 user messages** in total (9, 6, 5, 6 and 4 per chat). The
user's messages are scripted, so every method hears exactly the same thing; the model's
replies are generated live. Each reply is capped at about 250 words by the system prompt.

The plan covers **21 subjects**, such as the business goal, budget, team size, privacy law,
product approach, database, pricing, reminders, launch date, pilot clinics, payment gateway,
analytics tool and team roles. It is built to be hard to hand over:

- **11 of the 21 subjects change** at least once, and pricing changes twice
  (Rs 999 → Rs 1,499 → Rs 1,299).
- **Changes come late:** for example, the product approach chosen in chat 1 (a native
  Android app) is reversed in chat 3 (a WhatsApp booking bot), and the payment gateway chosen
  in chat 1 changes in chat 5.
- **Reasons are given for changes** ("Firestore costs are unpredictable", "patients won't
  install another app", "faster settlement to clinics").
- **An open question** ("one WhatsApp number per clinic, or a shared one?") is raised in
  chat 3 and answered in chat 5.
- **Planning discussion that decides nothing** (booking flow, onboarding, risks, training)
  sits between the decisions, as in a real chat. 11 of the 30 messages are like this.

How long the conversation gets, measured as the full previous transcript at the start of each
chat:

| Start of chat | 2 | 3 | 4 | 5 |
|---|---|---|---|---|
| Full previous transcript | 4,113 tokens | 7,122 | 9,704 | 12,853 |

### How each method runs

**Summary.** At the end of chats 1–4, the user sends: *"Summarize this conversation so I can
continue the project in a new chat. Include everything I'll need."* The next chat starts
with: *"Here is where our project stands: (summary) Let's continue."*

**ctx.** The chat has the six ctx tools and the ctx server's instructions, exactly as a chat
app gets them from the server. The tool calls run against a local ctx server with a fresh
database.

- At the end of chat 1 the user sends: *"Create a ctx repo called … and commit the project
  state from this chat."* At the end of chats 2–4: *"Commit the project state from this chat
  to the ctx repo …"* The model calls `ctx_commit` once with what was decided or changed since
  the last save (on the first save, everything decided so far), naming stored items by
  subject. The server matches the items to stored ones, replaces changed values, drops
  duplicates and flags conflicts; no lookup comes first. Reviews that need approval are
  confirmed the way the in-chat review view does it: the user's Confirm calls the server
  directly, with no model call, and a short note goes into the model's context.
- Each new chat starts with: *"Pull … with token_budget N"*, where N is the recommended size
  for the repo at that point (every item included, shortened where possible). The model calls
  `ctx_pull` and replies. Each line of the pull starts with the item's subject, so a later
  save can reuse it.
- Tools are used only when the user asks: to save at the end of a chat, and to pull at the
  start. During the planning messages, the tool definitions are still part of every request
  and their exact cost is charged (622 tokens per call for the six definitions, counted by the
  model's own token counter, plus 65 for the server instructions), but the model does not
  call tools there. This matches the product's rules: the instructions say to use ctx only
  when asked, and the server refuses saves the user did not ask for.

**Full transcript.** Each new chat starts with: *"Here is our conversation so far: (every
earlier message) Let's continue."*

### How knowledge is measured

At the start of chats 2–5, right after the handoff, a **copy** of the new chat is asked a
fixed list of questions, one line per answer. The copy is discarded, so the questions don't
influence the rest of the run, and their tokens are not counted in the totals.

The questions cover everything decided so far: 17 questions after handoff 1, 22 after
handoff 2, 26 after handoff 3 and 29 after handoff 4 (94 in total).

| Kind | Example | Count |
|---|---|---|
| Current value | "Which database do we use?" | 77 |
| Reason for a change | "Why did we switch to the current database?" | 9 |
| What came before | "Which database did we use before the current one?" | 8 |

Each answer is scored automatically against the answer key, using the keywords of the correct
value:

- **correct:** the current value is in the answer;
- **outdated:** an old value is stated and the current one is not;
- **missing:** neither (for example "unknown").

### How tokens are measured

Every number comes from the model API's own usage report for each call:

- **in:** all input read for the call, including any part served from the provider's cache.
  Every call re-reads the whole conversation so far, the system prompt and, for ctx, the tool
  definitions;
- **out:** the visible reply, including any tool call it writes;
- **think:** the model's thinking tokens;
- **total = in + out + think.**

## Results

### Tokens

| | Summary | ctx | Full transcript |
|---|---|---|---|
| Planning messages (the 30 scripted turns) | 83,654 | 93,949 | 248,953 |
| Starting each new chat (paste or pull) | 5,263 | 13,829 | 39,572 |
| Handoffs (writing the summary, or committing) | 18,486 | 47,003 | 0 |
| **Total** | **107,403** | **154,781** | **288,525** |
| Model calls | 38 | 46 | 34 |

Per chat:

| Chat | Summary | ctx | Full transcript |
|---|---|---|---|
| 1 | 29,554 | 43,981 | 24,681 |
| 2 | 22,627 | 32,816 | 45,719 |
| 3 | 20,574 | 30,828 | 57,129 |
| 4 | 23,671 | 32,090 | 86,389 |
| 5 | 10,977 | 15,066 | 74,607 |

Context each new chat started with:

| Start of chat | 2 | 3 | 4 | 5 |
|---|---|---|---|---|
| Summary | 428 | 456 | 458 | 433 |
| ctx | 413 | 567 | 714 | 768 |
| Full transcript | 4,113 | 7,122 | 9,704 | 12,853 |

### Knowledge

Correct answers after each handoff:

| After handoff | Summary | ctx | Full transcript |
|---|---|---|---|
| 1 (17 questions) | 10 | **17** | 16 |
| 2 (22 questions) | 11 | 19 | **21** |
| 3 (26 questions) | 12 | **22** | **22** |
| 4 (29 questions) | 14 | 23 | **27** |

By kind of question, over all four handoffs:

| | Summary | ctx | Full transcript |
|---|---|---|---|
| Current values | 46/77 | **73/77** | **73/77** |
| Reasons for changes | 1/9 | 0/9 | **6/9** |
| What came before | 0/8 | **8/8** | 7/8 |
| **All questions** | **47/94 (50%)** | **81/94 (86%)** | **86/94 (91%)** |
| Old values stated as current | 0 | 0 | 0 |

ctx's four missed current values: the DLT registration status (three times; the update
"registration approved" in chat 4 was not saved as a change to that item) and the WhatsApp
number decision made in chat 5 (once).

### Context decay

Context decay is what happens to early facts when a project is passed along a chain of
chats. To measure it, take the 8 facts settled in chat 1 and never changed afterwards (the
goal, team size, budget, privacy law, where data is stored, the backend, the hosting region
and the reminder times) and check whether each new chat still knows them:

| Still known after | Handoff 1 | Handoff 2 | Handoff 3 | Handoff 4 |
|---|---|---|---|---|
| Summary | 6/8 | 5/8 | 3/8 | 4/8 |
| ctx | **8/8** | **8/8** | **8/8** | **8/8** |
| Full transcript | 8/8 | 8/8 | 8/8 | 8/8 |

By handoff 4, the summary chain had lost the goal, the team size, the budget and the reminder
times. The new chat answered "unknown" for three of them and replaced the goal ("50 paying
clinics within 6 months") with a vague description of the product. Nothing in the
conversation had changed or contradicted any of these facts: they faded out.

**Why summaries decay:**

1. **Each summary is made from the previous summary.** Chat 3's summary is written from
   chat 2, which only ever saw chat 1 through its summary. A fact left out once cannot come
   back.
2. **Summaries stay about the same size while the project grows.** Here they stayed between
   428 and 458 tokens. To fit new decisions, older facts get compressed or dropped.
3. **Recent material wins.** The latest chat is in full detail in front of the model; the
   first chat survives as a line or two, so it is the first to go.

**Why ctx does not:**

- Every fact is stored as its own item and stays there until something explicitly changes it:
  a new value on the same subject, or an update marking it resolved or retracted. Nothing is
  rewritten, so nothing is summarized away.
- Every pull is built from the stored items, not from the previous chat. Chat 5 gets chat 1's
  facts from the source, exactly as chat 2 did.
- Changes keep what they replaced ("Use Supabase (was: Firebase)"), which is why ctx answered
  all 8 "what did we use before?" questions and the summary chain answered none.

**Where ctx can still lose a fact, and how it shows:**

| Case | What happens | Recovery |
|---|---|---|
| The model did not commit a fact when saving | The fact was never stored | Commit it later; nothing else is affected |
| A fact was replaced by mistake | Shown as a replacement in the review before anything is saved | The old item stays in history; revert restores it |
| A small pull size leaves out low-priority items | The items are still stored, and the pull lists what it left out | Pull a larger size |

In ctx a missing fact is visible and recoverable. In a summary chain it is lost silently and
for good.

## What the results mean

**Summaries are cheap but lose the project.** Each new summary is written from a chat that
began with the previous summary, so anything left out once is gone for good (see
[Context decay](#context-decay)). By the second handoff, the summaries no longer mentioned
the team size, the budget or the product approach, and none of them recorded what a decision
replaced. Their size stays flat (about 430–460 tokens) because the model keeps rewriting the
same amount, not because nothing was added.

**ctx keeps the project at a fraction of the transcript's size.** Every decision is stored as
its own item, and a change replaces the old item while keeping it in history, so the next chat
gets the current value plus "was: …". ctx answered as many current-value questions as the full
transcript (73 of 77 each), and more "what came before" questions, from a context about
one-seventeenth its size.

**Where ctx's tokens go.** ctx costs more than a summary for two reasons:

1. **Tool definitions** go with every request while the connector is on: about 690 tokens on
   each of the 46 calls.
2. **Saving takes two calls** (commit, then reply), and each one re-reads the chat. Writing a
   summary takes one. Handoffs are 47,003 of ctx's 154,781 tokens.

The full transcript costs the most, and its cost grows fastest: every later message re-reads
an ever longer history (4,113 tokens of carried context at chat 2, 12,853 at chat 5).

**Reasons are the weak spot for every compact method.** Only the full transcript keeps them
well (6/9), because it contains the user's original words. With ctx, the model has to copy the
user's "because…" into an item's reason field when it commits, and the model used here
did not. The summary method kept 1/9.

## Earlier version: saving with a lookup

An earlier version of ctx saved in three calls: the model first read every stored item with
`ctx_stored` (a list that grows with the repo), then committed, then replied. On the same
benchmark it used **201,095 tokens** (handoffs 86,248) and answered **88%** of the
questions, with 8/8 chat-1 facts kept and 7/8 history.

The current version saves in two calls with no lookup: **23% fewer tokens in total** and
**45% fewer for saving**, at about the same accuracy (86%) with the same zero decay. Its save
cost also no longer grows with the number of stored items. The first attempt at this version
used shorter commit rules, and the model bundled several facts into one item (13 items on the
first save instead of 21), which dropped accuracy to 81%; the commit rules now ask for one
item per fact with an example, which restored it.

## Limitations

- **One model:** Gemini 3 Flash (`gemini-3-flash-preview`) on Vertex AI, standing in for the
  user's chat model. Other models extract, summarize and follow tool rules differently.
- **Scripted user:** the user's messages are fixed. Real users phrase things differently,
  repeat themselves and correct the model.
- **Run-to-run variation:** the model's replies differ between runs. Repeated runs moved the
  totals by roughly ±10%, without changing the order of the methods. The summary and
  full-transcript numbers here come from one run and the ctx numbers from another, on the same
  scenario; neither the summary nor the full-transcript method uses ctx's code.
- **Keyword scoring:** answers are scored by keywords of the correct and old values. An answer
  that is right but worded without the keyword counts as missing.
- **Not claude.ai:** a chat app adds its own system prompt and features, which this benchmark
  does not include. Those are the same for every method, so they don't change the comparison.

## Reproducing it

Requirements: Docker running (for the throwaway database) and a Google Cloud project with
Vertex AI (the run uses a small amount of credit).

```powershell
uv run python -m evals.handoff_chain --arms summary,full --project <gcp-project-id>
uv run python -m evals.handoff_chain --arms ctx --no-unprompted --pull-size recommended --project <gcp-project-id>
```

Add `--size small` for a 3-chat run.

| File | What it holds |
|---|---|
| `evals/handoff_scenario.py` | The 5 chats, the 21 subjects, the answer key and the scoring |
| `evals/handoff_chain.py` | The runner: chats, handoffs, probes, token accounting and the console report |
| `evals/results/handoff-chain-<time>/console.txt` | The full console log of a run |
| `evals/results/handoff-chain-<time>/results.json` | Every number, every probe answer and its score |
| `evals/results/handoff-chain-<time>/<method>-transcripts.json` | Every message, reply and tool call |

The numbers in this document come from `handoff-chain-20261110-181901` (summary and full
transcript) and `handoff-chain-20261111-120847` (ctx). The earlier version with the lookup is
`handoff-chain-20261110-194259`.
