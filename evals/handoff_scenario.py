"""Scripted planning project for the handoff-chain benchmark (evals/handoff_chain.py).

ClinicSlot, an appointment-booking product, planned over five chats. The user's messages are
fixed so every arm hears exactly the same thing; the assistant's replies are generated live.
Decisions change across chats (some three chats later), reasons are given for the changes,
an open question is raised and later answered, and planning discussion that changes nothing
sits between the decisions, as in a real chat.

Each message that states a value is tagged with `sets`, which builds the answer key: after
any chat, the current value of every subject, what it replaced, and why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Value:
    label: str  # shown in logs
    keywords: tuple[str, ...]  # all must appear in an answer (regex, case-insensitive)


@dataclass(frozen=True)
class Subject:
    key: str
    question: str
    values: tuple[Value, ...]
    # reason given when the subject changed to a value: (value index, question, keywords)
    reasons: tuple[tuple[int, str, tuple[str, ...]], ...] = ()
    history_question: str | None = None  # "what did we use before ...?"


def V(label: str, *keywords: str) -> Value:
    return Value(label, keywords or (re.escape(label.lower()),))


SUBJECTS: dict[str, Subject] = {
    s.key: s
    for s in [
        Subject(
            "goal",
            "What is the business goal?",
            (V("50 paying clinics in 6 months", r"\b50\b", r"\b6 months|six months"),),
        ),
        Subject("budget", "What is the first-year budget?", (V("Rs 3 lakh", r"3 lakh"),)),
        Subject(
            "team",
            "How big is the development team?",
            (V("2 developers", r"\b(2|two) (developers|devs|engineers)"),),
        ),
        Subject(
            "compliance",
            "Which data-protection law must we comply with?",
            (V("DPDP Act", r"dpdp"),),
        ),
        Subject("residency", "Where must patient data be stored?", (V("India", r"india"),)),
        Subject(
            "approach",
            "Which product approach did we choose?",
            (V("native Android app", r"android"), V("WhatsApp-first booking bot", r"whatsapp")),
            reasons=((1, "Why did we choose the current product approach?", (r"install",)),),
            history_question="Which product approach did we have before the current one?",
        ),
        Subject("backend", "What is the backend built with?", (V("NestJS", r"nest"),)),
        Subject(
            "database",
            "Which database do we use?",
            (V("Firebase / Firestore", r"fire(base|store)"), V("Supabase Postgres", r"supabase")),
            reasons=(
                (1, "Why did we switch to the current database?", (r"unpredictable|awkward",)),
            ),
            history_question="Which database did we use before the current one?",
        ),
        Subject("region", "Which cloud region hosts the data?", (V("Mumbai", r"mumbai"),)),
        Subject(
            "pricing",
            "What do clinics with 2-3 doctors pay per month?",
            (V("Rs 999", r"\b999\b"), V("Rs 1,499", r"1,?499"), V("Rs 1,299", r"1,?299")),
            reasons=((1, "Why was the Rs 999 price dropped?", (r"too high|expensive",)),),
        ),
        Subject(
            "free_tier",
            "Is there a free plan, and what does it include?",
            (V("free for 1 doctor up to 100 bookings", r"100 bookings"),),
        ),
        Subject(
            "reminders",
            "Through which channel are reminders sent?",
            (V("SMS", r"\bsms\b"), V("WhatsApp via MSG91, SMS fallback", r"whatsapp", r"fallback")),
        ),
        Subject(
            "reminder_times",
            "When are reminders sent?",
            (V("24 h and 2 h before", r"24", r"\b2 ?(h|hours)"),),
        ),
        Subject(
            "launch",
            "When do we launch?",
            (V("15 March 2027", r"march"), V("1 April 2027", r"april")),
            history_question="What launch date did we have before the current one?",
        ),
        Subject(
            "pilot",
            "How many pilot clinics, and where?",
            (
                V("10 clinics, Kothrud and Baner", r"\b10\b", r"kothrud"),
                V("20 clinics, Kothrud, Baner and Wakad", r"\b20\b", r"wakad"),
            ),
        ),
        Subject(
            "payments",
            "Which payment gateway do we use?",
            (V("Razorpay", r"razorpay"), V("Cashfree", r"cashfree")),
            reasons=((1, "Why did we switch payment gateways?", (r"settlement",)),),
            history_question="Which payment gateway did we use before the current one?",
        ),
        Subject(
            "analytics",
            "Which product-analytics tool do we use?",
            (V("Mixpanel", r"mixpanel"), V("PostHog", r"posthog")),
            reasons=((1, "Why did we switch analytics tools?", (r"self.?host",)),),
            history_question="Which analytics tool did we use before the current one?",
        ),
        Subject(
            "riya",
            "What does Riya work on?",
            (V("the Flutter app", r"flutter"), V("the React dashboard", r"dashboard")),
        ),
        Subject(
            "dlt",
            "What is the status of DLT registration?",
            (V("started", r"start|progress|pending|submitted"), V("approved", r"approved")),
        ),
        Subject(
            "wa_number",
            "One WhatsApp number per clinic, or one shared number?",
            (
                V("undecided", r"undecided|open|not (yet )?decided"),
                V("one shared number with a clinic code", r"shared"),
            ),
        ),
        Subject(
            "wa_content",
            "What may WhatsApp messages contain about a patient?",
            (V("only first name and appointment time", r"first name"),),
        ),
    ]
}


@dataclass
class Msg:
    text: str
    sets: dict[str, int] = field(default_factory=dict)


# Planning discussion that changes no subject (checked below: never states a value keyword).
def F(text: str) -> Msg:
    return Msg(text)


CHATS: list[list[Msg]] = [
    [  # chat 1: the first plan
        Msg(
            "I'm planning ClinicSlot, an appointment-booking product for small clinics (1-3 "
            "doctors) in Pune. The goal is 50 paying clinics within 6 months of launch. "
            "Constraints: a team of 2 developers, a first-year budget of Rs 3 lakh, and we must "
            "comply with India's DPDP Act. Patient data must stay in India.",
            {"goal": 0, "team": 0, "budget": 0, "compliance": 0, "residency": 0},
        ),
        Msg(
            "Approach A: a native Android app for patients plus a web dashboard for clinics. "
            "Riya will build the Flutter app, Aayush the backend in NestJS.",
            {"approach": 0, "riya": 0, "backend": 0},
        ),
        Msg(
            "Database and auth: Firebase (Firestore + Firebase Auth), hosted in Mumbai.",
            {"database": 0, "region": 0},
        ),
        F("What should the booking flow look like, step by step, for a patient?"),
        Msg(
            "Pricing: Rs 999 per clinic per month. Reminders go by SMS, 24 hours and 2 hours "
            "before the slot.",
            {"pricing": 0, "reminders": 0, "reminder_times": 0},
        ),
        F("How should a clinic onboard its doctors and weekly hours?"),
        Msg(
            "Payments through Razorpay, and Mixpanel for product analytics.",
            {"payments": 0, "analytics": 0},
        ),
        Msg(
            "We launch on 15 March 2027 with 10 pilot clinics in Kothrud and Baner.",
            {"launch": 0, "pilot": 0},
        ),
        F("What are the biggest risks in this plan?"),
    ],
    [  # chat 2: database and pricing change
        Msg(
            "I've changed my mind about Firebase: Firestore makes slot-availability queries "
            "awkward and its costs are unpredictable. Switch to Supabase Postgres in Mumbai.",
            {"database": 1},
        ),
        F("How should we model doctor leave days so they block slots?"),
        Msg(
            "Pilot feedback: Rs 999 is too high for single-doctor clinics. New pricing: free for "
            "1 doctor up to 100 bookings a month, and Rs 1,499 a month for clinics with 2-3 "
            "doctors.",
            {"pricing": 1, "free_tier": 0},
        ),
        Msg(
            "Reminders: use the WhatsApp Business API through MSG91, with SMS only as a "
            "fallback. Aayush has started the DLT registration for SMS.",
            {"reminders": 1, "dlt": 0},
        ),
        F("What should the reminder message text say, roughly?"),
        F("How do we handle a patient who cancels less than an hour before the slot?"),
    ],
    [  # chat 3: approach reversed
        Msg(
            "Final decision: drop approach A. Patients won't install another app. Go with "
            "WhatsApp-first booking: patients book by chatting with a WhatsApp bot, clinics use "
            "the React web dashboard. No Flutter app.",
            {"approach": 1},
        ),
        Msg(
            "Riya moves to the React dashboard, and the launch moves to 1 April 2027.",
            {"riya": 1, "launch": 1},
        ),
        Msg(
            "Open question, I haven't decided: one WhatsApp number per clinic, or one shared "
            "ClinicSlot number?",
            {"wa_number": 0},
        ),
        F("What conversation steps should the booking bot have?"),
        F("How should the clinic's screen show today's appointments?"),
    ],
    [  # chat 4: pilot, price, a new constraint, analytics
        Msg("The pilot grows to 20 clinics, adding Wakad to Kothrud and Baner.", {"pilot": 1}),
        Msg(
            "Pricing tweak: Rs 1,299 a month instead of Rs 1,499 for clinics with 2-3 doctors.",
            {"pricing": 2},
        ),
        Msg(
            "New rule: WhatsApp messages must never contain patient data beyond the first name "
            "and the appointment time.",
            {"wa_content": 0},
        ),
        Msg(
            "Switch analytics from Mixpanel to PostHog, because we can self-host it in India.",
            {"analytics": 1},
        ),
        Msg("Good news: the DLT registration is approved.", {"dlt": 1}),
        F("How should we train clinic receptionists to use ClinicSlot?"),
    ],
    [  # chat 5: payments, the open question answered
        Msg(
            "Switch payments from Razorpay to Cashfree for faster settlement to clinics.",
            {"payments": 1},
        ),
        Msg(
            "Decided: one shared ClinicSlot WhatsApp number, with a short clinic code at the "
            "start of the chat.",
            {"wa_number": 1},
        ),
        F("What should we measure during the pilot to know it's working?"),
        F("What's a sensible plan for the first month after launch?"),
    ],
]

SYSTEM = (
    "You are a helpful assistant helping the user plan a software product. Keep each answer "
    "under about 250 words."
)
SUMMARY_REQUEST = (
    "Summarize this conversation so I can continue the project in a new chat. Include "
    "everything I'll need."
)
COMMIT_REQUEST = "Commit the project state from this chat to the ctx repo {repo}."
FIRST_COMMIT_REQUEST = (
    "Create a ctx repo called {repo} and commit the project state from this chat."
)
PULL_REQUEST = "Pull {repo}."


@dataclass
class Probe:
    kind: str  # value | reason | history
    subject: str
    question: str
    expect: tuple[str, ...]  # keywords that must all appear
    outdated: list[tuple[str, ...]]  # keyword sets of replaced values
    label: str


def state_after(chat: int) -> dict[str, list[int]]:
    """Value history per subject after chats 0..chat (inclusive)."""
    hist: dict[str, list[int]] = {}
    for msgs in CHATS[: chat + 1]:
        for m in msgs:
            for k, v in m.sets.items():
                if not hist.get(k) or hist[k][-1] != v:
                    hist.setdefault(k, []).append(v)
    return hist


def probes_after(chat: int) -> list[Probe]:
    out: list[Probe] = []
    hist = state_after(chat)
    for key, h in hist.items():
        s = SUBJECTS[key]
        cur = s.values[h[-1]]
        old = [s.values[v].keywords for v in h[:-1] if v != h[-1]]
        out.append(Probe("value", key, s.question, cur.keywords, old, cur.label))
    for key, h in hist.items():
        s = SUBJECTS[key]
        for v, question, kws in s.reasons:
            if h[-1] == v or v in h:
                out.append(Probe("reason", key, question, kws, [], "reason"))
        if s.history_question and len(h) > 1:
            prev = s.values[h[-2]]
            out.append(Probe("history", key, s.history_question, prev.keywords, [], prev.label))
    return out


def probe_message(probes: list[Probe]) -> str:
    lines = [
        "Answer briefly from what you know about this project, one line per question, "
        'numbered exactly as below. If you don\'t know, say "unknown".',
    ]
    lines += [f"{i}. {p.question}" for i, p in enumerate(probes, 1)]
    return "\n".join(lines)


def _matches(text: str, keywords: tuple[str, ...]) -> bool:
    return all(re.search(k, text, re.IGNORECASE) for k in keywords)


def score(p: Probe, answer: str) -> str:
    """correct | outdated | missing."""
    if _matches(answer, p.expect):
        return "correct"
    if p.kind == "value" and any(_matches(answer, o) for o in p.outdated):
        return "outdated"
    return "missing"


def parse_answers(text: str, n: int) -> list[str]:
    found: dict[int, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*\**\s*(\d{1,2})[.)]\**\s*(.*)", line)
        if m and 1 <= int(m.group(1)) <= n and int(m.group(1)) not in found:
            found[int(m.group(1))] = m.group(2).strip()
    return [found.get(i, "") for i in range(1, n + 1)]


# The filler messages must never state a value, or they would leak answers into transcripts.
for _msgs in CHATS:
    for _m in _msgs:
        if not _m.sets:
            for _s in SUBJECTS.values():
                for _v in _s.values:
                    if len(_v.keywords) == 1 and _matches(_m.text, _v.keywords):
                        if _s.key not in {"reminder_times", "dlt", "wa_number", "approach"}:
                            raise AssertionError(f"filler states {_s.key}: {_m.text!r}")
