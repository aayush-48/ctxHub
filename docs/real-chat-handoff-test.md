
# Real-chat handoff test: Claude summary vs ctx

A test you run by hand in claude.ai to compare two ways of continuing a project in a new chat:

- **Arm S (summary):** ask Claude to summarize the chat, paste the summary into a new chat.
- **Arm C (ctx):** commit the chat's state with ctxHub, pull it into a new chat.

It measures **tokens carried**, **context retained** (correct current decisions, no outdated
ones) and **answer quality** in the new chat, over **two handoffs**. The second handoff is where
the two approaches really differ.

## Before you start

1. **Turn off Claude's memory and chat search** (Settings → Capabilities → "Search and reference
   chats" and "Memory"). Otherwise a new chat can look up the old one and the test is void.
   Turn them back on afterwards.
2. Don't use a Claude Project. Use plain chats.
3. Use the **same model** in every chat.
4. In the Arm S chats, **turn the ctxHub connector off** (the "+" or tools menu in the chat), so
   that arm doesn't pay for tool definitions it doesn't use. Keep it on in the Arm C chats.
5. Pick a unique repo name, for example `clinicslot-aayush`. Below it is `clinicslot`.
6. **Turn extended thinking off** in every test chat. claude.ai shows only a summary of the
   thinking, so real thinking tokens can't be measured, and thinking doesn't depend on which
   handoff method you use. If you keep it on, keep it on in both arms.
7. **Counting tokens.** claude.ai doesn't show token counts. Count with the script
   [evals/chat_tokens.py](../evals/chat_tokens.py). It uses o200k, which is within about 15%
   of Claude's own count; that's fine for comparing the two arms.
   - Any text file (a summary, a pulled context):
     `uv run python -m evals.chat_tokens summary1.txt`
   - Whole chats, the way the model reads them (each reply re-reads everything before it):
     export your data (Settings → Privacy → Export data), unzip it, then:
     ```powershell
     uv run python -m evals.chat_tokens conversations.json --match "S2" --tools 0
     uv run python -m evals.chat_tokens conversations.json --match "C2" --tools 1500
     ```
     Rename each test chat (S2, C2, S3, C3…) so `--match` finds it. `--tools 1500` adds
     ctxHub's tool definitions to each reply in the ctx chats.

On the free plan, this takes about 25 messages. If you hit the limit, continue the next day:
nothing in the test depends on timing.

---

## Step 1: the planning chat (chat 1, used by both arms)

Send these ten messages in order in **one** new chat. Start with the line in brackets so the
answers stay a realistic size.

The chat deliberately contains: a goal, constraints, decisions with reasons, an open question,
tasks, **three changes of mind** (database, pricing, launch date) and a **final approach (B) that
contradicts the first one (A)**.

**Q1: goal, constraints, approach A**
```
[Keep each answer under about 300 words.]
I'm planning ClinicSlot, an appointment-booking product for small clinics (1-3 doctors) in Pune.
Goal: 50 paying clinics within 6 months of launch. Constraints: a team of 2 developers, a total
budget of Rs 3 lakh for the first year, and we must comply with India's DPDP Act for patient data.
My first idea is approach A: a native Android app for patients plus a web dashboard for clinics.
Help me scope the MVP for approach A.
```

**Q2: stack**
```
Let's decide the stack: Flutter for the patient app, React for the clinic dashboard, and a
Node.js backend with NestJS. Agree? Point out the risks.
```

**Q3: database (will change later)**
```
For the database and auth, let's use Firebase (Firestore + Firebase Auth) to move fast. What
should the data model look like for clinics, doctors, slots and bookings?
```

**Q4: data location and privacy**
```
Patient data must stay in India, so we host in Mumbai (asia-south1). Patients log in with their
phone number, no email. How do we handle consent and data deletion requests under DPDP?
```

**Q5: pricing (will change later)**
```
Pricing: Rs 999 per clinic per month with a 14-day free trial. Is that reasonable for Pune
clinics? Compare it with Practo.
```

**Q6: reminders, plus an open question**
```
Reminders: SMS through MSG91, sent 24 hours and 2 hours before the slot. Open question: should
we also support WhatsApp reminders? I haven't decided yet.
```

**Q7: launch plan and tasks (date will change later)**
```
Launch target: 15 March 2027, with 10 pilot clinics in Kothrud and Baner. List the tasks for the
next 6 weeks and who does what: Aayush on the backend, Riya on the Flutter app.
```

**Q8: change of mind on the database**
```
I've changed my mind about Firebase: Firestore makes slot-availability queries awkward and the
costs are unpredictable. Switch to Supabase (Postgres) in the Mumbai region, with Supabase Auth
phone OTP. What changes in the data model and the plan?
```

**Q9: change of mind on pricing, and the open question answered**
```
Pilot feedback: Rs 999 is too high for single-doctor clinics. New pricing: free for 1 doctor up
to 100 bookings a month, and Rs 1,499 a month for clinics with 2-3 doctors. And yes to WhatsApp
reminders: use the WhatsApp Business API through MSG91 instead of SMS, with SMS only as a fallback.
```

**Q10: final approach B, which contradicts approach A**
```
Final decision: drop approach A. Patients won't install another app. Go with approach B:
WhatsApp-first booking, where patients book by chatting with the clinic's WhatsApp number (a
bot), and clinics use the React web dashboard. No Flutter app. The launch date moves to
1 April 2027. Tell me what changes and what we keep.
```

### The correct final state (the answer key)

| Subject | Current (correct) | Outdated (must NOT be stated as current) |
|---|---|---|
| Approach | B: WhatsApp-first booking bot + React clinic dashboard | A: native Android app |
| Patient app | None | Flutter app |
| Backend | Node.js with NestJS | |
| Database and auth | Supabase Postgres in Mumbai, phone OTP | Firebase / Firestore / Firebase Auth |
| Pricing | Free for 1 doctor up to 100 bookings/month; Rs 1,499/month for 2-3 doctors | Rs 999/month |
| Reminders | WhatsApp Business API via MSG91, SMS fallback; 24 h and 2 h before | SMS only |
| WhatsApp question | Answered: yes | Still open |
| Launch | 1 April 2027; 10 pilot clinics in Kothrud and Baner | 15 March 2027 |
| Goal | 50 paying clinics within 6 months of launch | |
| Constraints | 2 developers; Rs 3 lakh first-year budget; DPDP compliance; data stays in India | |
| Team | Aayush: backend. Riya: no longer Flutter (needs a new role) | Riya on the Flutter app |

**Measure:** save the whole chat 1 text (select it all and copy, or Settings → Export data) to
`transcript.txt` and count its tokens. This is what pasting the full chat would cost.

---

## Step 2: first handoff

### Arm S (summary)
1. In **chat 1**, send:
   ```
   Summarize this conversation so I can continue the project in a new chat. Include everything
   I'll need.
   ```
2. Copy the summary into `summary1.txt` and count its tokens.
3. Open **chat S2** (connector off). Paste the summary, then send the **probe message** below.

### Arm C (ctx)
1. In **chat 1**, send:
   ```
   Create a ctx repo called clinicslot and commit the project state from this chat to it.
   Show me the review before confirming.
   ```
2. **Check the review:** it should hold only the final state (approach B, Supabase, Rs 1,499,
   1 April). The rules tell Claude to commit only final choices. Note anything outdated or
   missing, then confirm.
3. Open **chat C2** (connector on). Send `Pull clinicslot`, pick **Recommended** in the view,
   click **Load into chat**. Write down the token number the view shows.
4. Send the **probe message** below.

### The probe message (same in both arms, one message)
```
Answer briefly, from what you know about this project:
1. Which product approach did we choose, and why?
2. What do patients need to install?
3. Which database and login method do we use?
4. What is the pricing?
5. When do we launch, and with which pilot clinics?
6. How do reminders work?
7. What are the project's constraints?
8. What is the goal?
9. What is still undecided?
10. What did we use before Supabase, and why did we switch?
Then write the week-1 sprint plan for both developers.
```

### Scoring
For each of questions 1-10:

| Score | Meaning |
|---|---|
| 2 | Correct and complete |
| 1 | Partly correct or incomplete |
| 0 | Doesn't know or says it isn't stated |
| **-1** | States an **outdated** value as current (for example Firebase, Rs 999, Flutter, 15 March) |

The **sprint plan**: one point for each of these, 0-6:
1. Builds the WhatsApp bot + React dashboard (no Flutter app).
2. Uses NestJS + Supabase (no Firebase).
3. Includes DPDP consent and deletion.
4. Gives Riya a new role (not Flutter).
5. Fits the 1 April 2027 launch.
6. Respects 2 developers and the Rs 3 lakh budget.

---

## Step 3: second handoff (where the difference shows)

Continue **in S2 and C2 with the same three messages**:

**R1**
```
Change: the pilot will be 20 clinics, adding Wakad to Kothrud and Baner.
```
**R2**
```
Pricing tweak: Rs 1,299 a month instead of Rs 1,499 for clinics with 2-3 doctors.
```
**R3**
```
New constraint: WhatsApp messages must never contain patient data beyond the first name and the
appointment time.
```

Then hand off again:

- **Arm S:** in S2, send the same summary request. Copy the result into `summary2.txt` and count
  it. Open **S3**, paste it, send the probe message.
- **Arm C:** in C2, send `Commit these changes to clinicslot and show me the review.`
  **Expect:** a **supersede** for the pilot (10 → 20 clinics) and the price (1,499 → 1,299), and
  an **add** for the new constraint. Confirm. Open **C3**, pull at Recommended, note the tokens,
  send the probe message.

For round 2, the answer key changes: pilot = 20 clinics in Kothrud, Baner and Wakad; price =
Rs 1,299 for 2-3 doctors (Rs 1,499 and Rs 999 are now both outdated); constraints add the
WhatsApp data rule. Add two probes to the message:
```
11. How many pilot clinics, and where?
12. What can WhatsApp messages contain?
```

---

## Step 4 (optional): a conflict between branches

Shows something a summary can't do at all.

1. In a chat with the connector on:
   ```
   In clinicslot, create a branch called app-first from main and commit approach A there: a
   Flutter Android app for patients, alongside the React dashboard.
   ```
2. Then:
   ```
   Merge app-first into main.
   ```
3. **Expect:** the conflict resolver shows the approach on main (B) and on app-first (A) side
   by side. Choose one, or edit. Nothing is overwritten silently.

---

## Results sheet

| | Arm S (summary) | Arm C (ctx) |
|---|---|---|
| Transcript of chat 1 (tokens) | | (same) |
| Context carried, handoff 1 (tokens) | summary1 = | pull = |
| Probe score, handoff 1 (out of 20) | | |
| Outdated values stated, handoff 1 | | |
| Sprint plan, handoff 1 (out of 6) | | |
| Context carried, handoff 2 (tokens) | summary2 = | pull = |
| Probe score, handoff 2 (out of 24) | | |
| Outdated values stated, handoff 2 | | |
| Manual steps (copy, paste, review) | | |
| Could you see what changed between handoffs? | | |

**Tokens over the next chat**, for N follow-up messages (each message re-reads the carried
context):

- Arm S: N × summary tokens
- Arm C: N × (pull tokens + ~1,500 for the ctxHub tool definitions)
- Full transcript, for reference: N × transcript tokens

## What to expect, honestly

- **Handoff 1:** the two arms will likely score close. A fresh Claude summary of one chat is
  good, and that is fine to report. Arm C's pull will usually be smaller than the summary.
- **Handoff 2:** this is where the arms tend to separate. S3 depends on a summary of a chat
  that itself began with a summary, so changed values (pilot size, price) are more likely to be
  missing or outdated. C3 reads the stored state, updated by a reviewed diff.
- **Tool overhead:** with the connector on, every message costs about 1,500 tokens for the
  tool definitions. For a short chat this can cancel out the smaller context. Report it.
