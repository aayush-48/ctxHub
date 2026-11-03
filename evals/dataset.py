"""Synthetic dataset: subjects with alternative values (and paraphrases of each value), used
to plant decisions, reversals, duplicates and branch splits with known gold outcomes."""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subject:
    name: str
    type: str
    question: str  # probe question for handoff evals
    values: tuple[tuple[str, ...], ...]  # each value: canonical text first, then paraphrases
    keywords: tuple[str, ...]  # one keyword per value, to score free-text answers


SUBJECTS: list[Subject] = [
    Subject(
        "Primary datastore",
        "decision",
        "Which database stores the project state?",
        (
            (
                "Use Cloud SQL Postgres with pgvector.",
                "Store everything in PostgreSQL on Cloud SQL with the pgvector extension.",
            ),
            ("Use MongoDB Atlas.", "Keep the state in a MongoDB Atlas cluster."),
            ("Use Firestore.", "Store the state in Google Firestore."),
        ),
        ("postgres", "mongo", "firestore"),
    ),
    Subject(
        "Deploy region",
        "fact",
        "Which cloud region is the service deployed in?",
        (
            ("The service runs in asia-south1.", "Deployment region: asia-south1 (Mumbai)."),
            ("The service runs in us-central1.", "Deployment region: us-central1 (Iowa)."),
            ("The service runs in europe-west1.", "Deployment region: europe-west1 (Belgium)."),
        ),
        ("asia-south1", "us-central1", "europe-west1"),
    ),
    Subject(
        "Background jobs",
        "decision",
        "What runs background jobs?",
        (
            ("Use Cloud Tasks for background jobs.", "Background work goes through Cloud Tasks."),
            ("Use Pub/Sub for background jobs.", "Background work goes through Pub/Sub topics."),
            ("Use Celery with Redis.", "Run background jobs on Celery backed by Redis."),
        ),
        ("cloud tasks", "pub/sub", "celery"),
    ),
    Subject(
        "Hosting budget",
        "constraint",
        "What is the hosting budget constraint?",
        (
            (
                "Hosting must stay under 50 dollars a month.",
                "Monthly hosting cost has to remain below $50.",
            ),
            (
                "Hosting must stay under 200 dollars a month.",
                "Monthly hosting cost has to remain below $200.",
            ),
        ),
        ("50", "200"),
    ),
    Subject(
        "Auth provider",
        "decision",
        "Which identity provider handles sign-in?",
        (
            ("Use Google sign-in.", "People sign in with their Google accounts."),
            ("Use Auth0.", "Sign-in is handled by Auth0."),
            ("Use GitHub sign-in.", "People sign in with their GitHub accounts."),
        ),
        ("google", "auth0", "github"),
    ),
    Subject(
        "CLI framework",
        "decision",
        "Which framework is the CLI built with?",
        (
            ("Build the CLI with Typer.", "The command-line tool uses Typer."),
            ("Build the CLI with Click.", "The command-line tool uses Click."),
            ("Build the CLI with argparse.", "The command-line tool uses argparse."),
        ),
        ("typer", "click", "argparse"),
    ),
    Subject(
        "Launch date",
        "goal",
        "When is the launch planned?",
        (
            ("Launch the beta in March.", "Ship the beta release in March."),
            ("Launch the beta in June.", "Ship the beta release in June."),
        ),
        ("march", "june"),
    ),
    Subject(
        "Embedding size",
        "fact",
        "What embedding dimension is used?",
        (
            ("Embeddings have 768 dimensions.", "Vectors are 768-dimensional."),
            ("Embeddings have 1536 dimensions.", "Vectors are 1536-dimensional."),
        ),
        ("768", "1536"),
    ),
    Subject(
        "Frontend stack",
        "decision",
        "What is the web viewer built with?",
        (
            ("Build the viewer with React.", "The web viewer is a React app."),
            ("Build the viewer with Svelte.", "The web viewer is a Svelte app."),
        ),
        ("react", "svelte"),
    ),
    Subject(
        "Test database",
        "decision",
        "What database do tests run against?",
        (
            (
                "Tests use a real Postgres in Testcontainers.",
                "Integration tests run on Postgres started by Testcontainers.",
            ),
            ("Tests use SQLite in memory.", "Integration tests run on in-memory SQLite."),
        ),
        ("testcontainers", "sqlite"),
    ),
    Subject(
        "Rate limit",
        "constraint",
        "What is the per-user rate limit?",
        (
            (
                "Each user is limited to 120 requests per minute.",
                "Per-user rate limit: 120 requests a minute.",
            ),
            (
                "Each user is limited to 30 requests per minute.",
                "Per-user rate limit: 30 requests a minute.",
            ),
        ),
        ("120", "30"),
    ),
    Subject(
        "Merge policy",
        "decision",
        "How are cross-branch contradictions handled?",
        (
            (
                "Cross-branch contradictions always go to the user.",
                "Never auto-resolve contradictions between branches; ask the user.",
            ),
            (
                "The latest commit wins across branches.",
                "When branches disagree, the newest commit wins.",
            ),
        ),
        ("user", "latest"),
    ),
]


def item(subject: Subject, value: int, paraphrase: int = 0) -> dict:
    return {
        "type": subject.type,
        "subject": subject.name,
        "content": subject.values[value][paraphrase],
    }


@dataclass
class PipelineCase:
    """A linear history of commits on one subject, with the gold final value."""

    subject: Subject
    commits: list[list[dict]]
    gold_value: int
    planted_duplicates: int = 0  # commits that only restate the current value
    paraphrase_duplicates: int = 0


@dataclass
class MergeCase:
    """Shared base, then each branch commits; gold says whether the subject conflicts."""

    base: list[dict]
    ours: list[dict]
    theirs: list[dict]
    conflict_subjects: set[str] = field(default_factory=set)
    gold_final: dict[str, int] = field(default_factory=dict)  # for non-conflicting subjects


def pipeline_cases(seed: int = 7, n: int = 40) -> list[PipelineCase]:
    rng = random.Random(seed)
    cases = []
    for _ in range(n):
        s = rng.choice(SUBJECTS)
        v = rng.randrange(len(s.values))
        commits = [[item(s, v)]]
        dups = paras = 0
        for _ in range(rng.randint(1, 4)):
            roll = rng.random()
            if roll < 0.25:
                commits.append([item(s, v)])  # exact restatement
                dups += 1
            elif roll < 0.5 and len(s.values[v]) > 1:
                commits.append([item(s, v, 1)])  # paraphrase of the current value
                paras += 1
            else:
                v = rng.choice([x for x in range(len(s.values)) if x != v])  # reversal
                commits.append([item(s, v)])
        cases.append(PipelineCase(s, commits, v, dups, paras))
    return cases


def merge_cases(seed: int = 11, n: int = 40) -> list[MergeCase]:
    rng = random.Random(seed)
    cases = []
    kinds = [
        "ours_only",
        "theirs_only",
        "same_change",
        "conflict",
        "untouched",
        "new_both_same",
        "new_both_diff",
        "new_one_side",
    ]
    for _ in range(n):
        case = MergeCase(base=[], ours=[], theirs=[])
        for s in rng.sample(SUBJECTS, 3):
            kind = rng.choice(kinds)
            b = rng.randrange(len(s.values))
            others = [x for x in range(len(s.values)) if x != b]
            if kind.startswith("new_"):
                # The subject does not exist at the merge base.
                if kind == "new_both_same":
                    case.ours.append(item(s, b))
                    case.theirs.append(item(s, b, 1 if len(s.values[b]) > 1 else 0))
                    case.gold_final[s.name] = b
                elif kind == "new_both_diff":
                    case.ours.append(item(s, b))
                    case.theirs.append(item(s, others[0]))
                    case.conflict_subjects.add(s.name)
                else:
                    case.theirs.append(item(s, b))
                    case.gold_final[s.name] = b
                continue
            case.base.append(item(s, b))
            if kind == "ours_only":
                case.ours.append(item(s, others[0]))
                case.gold_final[s.name] = others[0]
            elif kind == "theirs_only":
                case.theirs.append(item(s, others[0]))
                case.gold_final[s.name] = others[0]
            elif kind == "same_change":
                case.ours.append(item(s, others[0]))
                case.theirs.append(item(s, others[0]))
                case.gold_final[s.name] = others[0]
            elif kind == "conflict" and len(others) >= 2:
                case.ours.append(item(s, others[0]))
                case.theirs.append(item(s, others[1]))
                case.conflict_subjects.add(s.name)
            else:
                case.gold_final[s.name] = b
        if not case.base:  # every repo needs at least one shared commit to fork from
            case.base.append({"type": "goal", "subject": "Eval goal", "content": "Ship it."})
        cases.append(case)
    return cases


def transcript_for(case_subjects: list[tuple[Subject, list[int]]], rng: random.Random) -> str:
    """A simulated conversation that states, and sometimes reverses, each subject's value."""
    fillers = [
        "Sounds good.",
        "Let me think about the trade-offs.",
        "Okay, noted.",
        "We can revisit later.",
        "That makes sense for an MVP.",
    ]
    lines = []
    for s, history in case_subjects:
        for n, v in enumerate(history):
            text = s.values[v][0]
            if n == 0:
                lines.append(f"User: For {s.name.lower()}: {text}")
            else:
                lines.append(
                    f"User: Change of plan on {s.name.lower()}. {text} Forget the earlier choice."
                )
            lines.append(f"Assistant: {rng.choice(fillers)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- long handoff

# Subjects whose answer keywords are distinctive enough to score free-text answers
# ("user" and "latest" from the merge-policy subject appear in ordinary answers).
HANDOFF_SUBJECTS = [s for s in SUBJECTS if s.name != "Merge policy"]

# Realistic side discussion that never states any subject's value. Checked below so filler
# can never leak an answer keyword into a transcript.
_FILLER_USER = [
    "Can you walk me through how error handling should work in the API layer?",
    "I'm worried the onboarding flow has too many steps for new people.",
    "What naming convention should we use for the internal modules?",
    "How should we structure the logging so it is easy to search later?",
    "Let's talk about how we version the public endpoints.",
    "Should the settings page be split into several smaller pages?",
    "What's a sensible retry policy for calls to external services?",
    "I keep going back and forth on how verbose the commit messages should be.",
    "How do we keep the documentation from drifting away from the code?",
    "Can we make the empty states in the interface more helpful?",
    "What should happen when someone loses their network connection mid-request?",
    "I'd like the error messages to tell people what to do next.",
    "How should we handle timezones when we show dates?",
    "Is it worth adding keyboard shortcuts this early?",
    "What's the cleanest way to share types between the services?",
]
_FILLER_ASSISTANT = [
    "A good approach is to keep each layer responsible for one kind of failure and to "
    "translate errors at the boundary, so callers always get a consistent shape.",
    "Fewer steps usually helps. Ask only for what is needed to get started, and defer the "
    "rest until people actually need it.",
    "Short, descriptive names grouped by feature tend to age better than names grouped by "
    "technical layer, because features are what people search for.",
    "Structured logs with a request identifier on every line make it far easier to follow "
    "one request across several services.",
    "Versioning in the path is the most visible option; whichever you choose, document the "
    "deprecation policy so integrators know what to expect.",
    "Splitting it by task rather than by data model usually makes it easier to find things.",
    "Exponential backoff with jitter and a small cap on attempts is a safe default, plus a "
    "clear error once the attempts run out.",
    "A short subject line and an optional body explaining why the change was made is a "
    "common and readable convention.",
    "Keeping examples in the docs executable, and checking them in continuous integration, "
    "is one of the few things that reliably prevents drift.",
    "An empty state should explain what belongs there and offer the one action that fills "
    "it, rather than just saying that nothing exists.",
    "Saving work locally and retrying when the connection returns avoids losing input, as "
    "long as people can see that a retry is pending.",
    "Store times in coordinated universal time and convert at display time, using the "
    "viewer's own setting.",
    "Shortcuts can wait until the main flows are stable, but reserving the common ones now "
    "avoids conflicts later.",
    "Generating the shared types from one schema keeps services in sync without copying "
    "definitions by hand.",
]


def _all_keywords() -> set[str]:
    return {k.lower() for s in HANDOFF_SUBJECTS for k in s.keywords}


for _line in _FILLER_USER + _FILLER_ASSISTANT:
    _hits = [k for k in _all_keywords() if k in _line.lower()]
    assert not _hits, f"filler leaks answer keywords {_hits}: {_line!r}"


@dataclass
class HandoffCase:
    """A multi-session project conversation. Values are introduced and changed across
    sessions, surrounded by unrelated discussion, so the full transcript is far larger than
    any budget and the latest value of a subject can be sessions away from its first."""

    history: list[tuple[Subject, list[int]]]  # value history per subject; last is gold
    sessions: list[list[tuple[str, str]]]  # per session: (role, text) messages

    def questions(self) -> list[str]:
        return [s.question for s, _ in self.history]


def handoff_case(
    rng: random.Random, n_subjects: int = 6, n_sessions: int = 4, filler_per_session: int = 40
) -> HandoffCase:
    subs = rng.sample(HANDOFF_SUBJECTS, n_subjects)
    events: dict[int, list[tuple[Subject, int, bool]]] = {k: [] for k in range(n_sessions)}
    history = []
    for s in subs:
        session = rng.randrange(n_sessions - 1)
        values = [rng.randrange(len(s.values))]
        events[session].append((s, values[0], False))
        for _ in range(rng.choice([0, 1, 1, 2])):  # most subjects change at least once
            if session >= n_sessions - 1:
                break
            session = rng.randrange(session + 1, n_sessions)
            v = rng.choice([x for x in range(len(s.values)) if x != values[-1]])
            values.append(v)
            events[session].append((s, v, True))
        history.append((s, values))

    sessions = []
    for k in range(n_sessions):
        msgs: list[tuple[str, str]] = []
        for _ in range(filler_per_session):
            msgs.append(("user", rng.choice(_FILLER_USER)))
            msgs.append(("assistant", rng.choice(_FILLER_ASSISTANT)))
        for s, v, changed in events[k]:
            text = s.values[v][rng.randrange(len(s.values[v]))]
            if changed:
                stmt = (
                    f"Change of plan on {s.name.lower()}: {text} This replaces what we said before."
                )
            else:
                stmt = f"Let's settle {s.name.lower()}. {text}"
            at = rng.randrange(len(msgs) // 2 + 1) * 2
            msgs[at:at] = [("user", stmt), ("assistant", "Understood, I'll go with that.")]
        sessions.append(msgs)
    return HandoffCase(history=history, sessions=sessions)


def render_transcript(sessions: list[list[tuple[str, str]]]) -> str:
    parts = []
    for n, msgs in enumerate(sessions, 1):
        parts.append(f"--- Session {n} ---")
        parts += [f"{'User' if r == 'user' else 'Assistant'}: {t}" for r, t in msgs]
    return "\n".join(parts)
