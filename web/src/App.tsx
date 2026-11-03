import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Branch, type Commit, type Item } from "./api";
import { handleCallback, logout } from "./auth";
import { Graph } from "./Graph";

// ---------------------------------------------------------------- tiny router

function usePath(): [string, (to: string) => void] {
  const [path, setPath] = useState(location.pathname + location.search);
  useEffect(() => {
    const on = () => setPath(location.pathname + location.search);
    addEventListener("popstate", on);
    return () => removeEventListener("popstate", on);
  }, []);
  const go = useCallback((to: string) => {
    history.pushState(null, "", to);
    setPath(to);
  }, []);
  return [path, go];
}

function useLoad<T>(fn: () => Promise<T>, deps: unknown[]): [T | undefined, string | undefined, () => void] {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setError(undefined);
    fn().then((d) => live && setData(d)).catch((e) => live && setError(e.message));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return [data, error, () => setN((x) => x + 1)];
}

const short = (id?: string | null) => (id || "").replace(/-/g, "").slice(0, 8);
const TYPE_ORDER = ["constraint", "goal", "decision", "open_question", "task", "fact"];
const label = (t: string) => t.replace("_", " ");

// ---------------------------------------------------------------- app

export function App() {
  const [path, go] = usePath();
  const [me, setMe] = useState<{ email: string }>();
  const [authError, setAuthError] = useState<string>();

  useEffect(() => {
    if (location.pathname.startsWith("/app/callback")) {
      handleCallback().then((to) => go(to)).catch((e) => setAuthError(e.message));
      return;
    }
    api("/me").then(setMe).catch((e) => setAuthError(e.message));
  }, [go]);

  if (authError) return <Shell><div className="empty">{authError} <a href="/app/">Retry</a></div></Shell>;
  if (!me) return <Shell><div className="empty">Loading…</div></Shell>;

  const m = path.match(/^\/app\/r\/([^/?]+)/);
  return (
    <Shell email={me.email} onHome={() => go("/app/")}>
      {m ? <RepoView repo={decodeURIComponent(m[1])} />
        : <OpenRepo open={(name) => go(`/app/r/${encodeURIComponent(name)}`)} />}
    </Shell>
  );
}

function Shell(props: { email?: string; onHome?: () => void; children: React.ReactNode }) {
  return (
    <div className="shell">
      <header className="top">
        <button className="brand" onClick={props.onHome}>ctx</button>
        <span className="tag">version control for LLM context</span>
        <span className="spacer" />
        {props.email && <>
          <span className="who">{props.email}</span>
          <button className="link" onClick={logout}>Sign out</button>
        </>}
      </header>
      <main>{props.children}</main>
    </div>
  );
}

// Repos opened in this browser, newest first. A per-browser convenience only.
const RECENT_KEY = "ctx.recentRepos";

function recentRepos(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch {
    return [];
  }
}

function rememberRepo(name: string) {
  try {
    const next = [name, ...recentRepos().filter((n) => n.toLowerCase() !== name.toLowerCase())];
    localStorage.setItem(RECENT_KEY, JSON.stringify(next.slice(0, 8)));
  } catch {
    /* storage unavailable: nothing to remember */
  }
}

function OpenRepo({ open }: { open: (name: string) => void }) {
  const [name, setName] = useState("");
  const recent = recentRepos();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (name.trim()) open(name.trim());
  };
  return (
    <section>
      <h1>Open a repository</h1>
      <p className="muted">Enter the name of a repository you own or that was shared with you.</p>
      <form className="share" onSubmit={submit}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-project"
               aria-label="Repository name" autoFocus />
        <button type="submit" className="primary">Open</button>
      </form>
      {recent.length > 0 && (
        <>
          <h3>Recently opened</h3>
          <div className="cards">
            {recent.map((r) => (
              <button key={r} className="card repo" onClick={() => open(r)}>
                <strong>{r}</strong>
              </button>
            ))}
          </div>
        </>
      )}
      <p className="muted small">
        Create a repository from a chat (“create a ctx repo called my-project”) or with
        <code> ctx init my-project</code>.
      </p>
    </section>
  );
}

type Tab = "history" | "state" | "merges" | "members";

function RepoView({ repo }: { repo: string }) {
  const [tab, setTab] = useState<Tab>("history");
  const [info, error] = useLoad<any>(() => api(`/repos/${encodeURIComponent(repo)}`), [repo]);
  useEffect(() => { if (info) rememberRepo(info.name); }, [info]);
  if (error) return <div className="error">{error}</div>;
  if (!info) return <div className="empty">Loading…</div>;
  return (
    <section>
      <div className="repo-head">
        <h1>{info.name}</h1>
        <span className="muted">{info.owner} · you are {info.role}</span>
      </div>
      <nav className="tabs" role="tablist">
        {(["history", "state", "merges", "members"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""}
                  onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>
      {tab === "history" && <History repo={repo} />}
      {tab === "state" && <State repo={repo} branches={info.branches} />}
      {tab === "merges" && <Merges repo={repo} />}
      {tab === "members" && <Members repo={repo} canEdit={info.role === "owner"} />}
    </section>
  );
}

function History({ repo }: { repo: string }) {
  const [log, error] = useLoad<{ commits: Commit[]; branches: Branch[] }>(
    () => api(`/repos/${repo}/log?all=true&limit=300`), [repo]);
  const [sel, setSel] = useState<string>();
  useEffect(() => { if (log && !sel && log.commits[0]) setSel(log.commits[0].id); }, [log, sel]);
  if (error) return <div className="error">{error}</div>;
  if (!log) return <div className="empty">Loading…</div>;
  return (
    <div className="split">
      <Graph commits={log.commits} branches={log.branches} selected={sel} onSelect={setSel} />
      {sel && <CommitDetail repo={repo} id={sel} />}
    </div>
  );
}

const DIFF_KINDS: [string, string][] = [
  ["added", "+"], ["superseded", "~"], ["resolved", "✓"], ["retracted", "−"],
  ["reactivated", "↺"], ["removed", "−"],
];

function CommitDetail({ repo, id }: { repo: string; id: string }) {
  const [c, error] = useLoad<any>(() => api(`/repos/${repo}/commits/${id}`), [repo, id]);
  const [audit, setAudit] = useState(false);
  if (error) return <div className="error">{error}</div>;
  if (!c) return <div className="detail empty">Loading…</div>;
  const changes = DIFF_KINDS.flatMap(([k, sym]) => (c.diff[k] as Item[]).map((i) => ({ k, sym, i })));
  return (
    <article className="detail">
      <h2>{c.commit.message}</h2>
      <p className="muted">
        <code>{short(c.commit.id)}</code> · {c.commit.kind} · {c.commit.author}
        {c.commit.source_model && <> · {c.commit.source_client} {c.commit.source_model}</>}
        <br />{new Date(c.commit.created_at).toLocaleString()}
      </p>
      {changes.length === 0 ? <p className="muted">No item changes.</p> : (
        <ul className="diff">
          {changes.map(({ k, sym, i }) => (
            <li key={k + i.id} className={`d-${k}`}>
              <span className="sym" aria-label={k}>{sym}</span>
              <span className="type">{label(i.type)}</span>
              <span>{i.content}</span>
            </li>
          ))}
        </ul>
      )}
      {c.audit.length > 0 && (
        <>
          <button className="link" onClick={() => setAudit(!audit)}>
            {audit ? "Hide" : "Show"} pipeline decisions ({c.audit.length})
          </button>
          {audit && (
            <table className="audit">
              <tbody>
                {c.audit.map((a: any, n: number) => (
                  <tr key={n}><td>{a.stage}</td><td>{a.decision}</td>
                    <td><code>{JSON.stringify(a.details).slice(0, 200)}</code></td></tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </article>
  );
}

function State({ repo, branches }: { repo: string; branches: Branch[] }) {
  const [ref, setRef] = useState("main");
  const [all, setAll] = useState(false);
  const [data, error] = useLoad<{ items: Item[] }>(
    () => api(`/repos/${repo}/items?ref=${encodeURIComponent(ref)}&all=${all}`), [repo, ref, all]);
  return (
    <div>
      <div className="toolbar">
        <label>Branch{" "}
          <select value={ref} onChange={(e) => setRef(e.target.value)}>
            {branches.map((b) => <option key={b.name}>{b.name}</option>)}
          </select>
        </label>
        <label><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> include closed items</label>
      </div>
      {error && <div className="error">{error}</div>}
      {data && data.items.length === 0 && <div className="empty">No items on this branch yet.</div>}
      {data && TYPE_ORDER.map((t) => {
        const rows = data.items.filter((i) => i.type === t);
        if (!rows.length) return null;
        return (
          <section key={t} className="group">
            <h3>{label(t)}s <span className="muted">{rows.length}</span></h3>
            <ul className="items">
              {rows.map((i) => (
                <li key={i.id} className={i.status !== "active" ? "closed" : ""}>
                  <div>{i.content}{i.pinned && <span className="pill">pinned</span>}
                    {i.status !== "active" && <span className="pill">{i.status}</span>}</div>
                  <div className="muted small">{i.subject}{i.reason && <> · {i.reason}</>} · <code>{short(i.item_key)}</code></div>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

// Read-only: conflicts are resolved from a chat (ctx_resolve) or the CLI.
function Merges({ repo }: { repo: string }) {
  const [pending, error] = useLoad<any[]>(async () => {
    const list = await api<any[]>(`/repos/${repo}/merges?state=pending`);
    return Promise.all(list.map((m) => api(`/merges/${m.merge_id}`)));
  }, [repo]);
  if (error) return <div className="error">{error}</div>;
  if (!pending) return <div className="empty">Loading…</div>;
  if (!pending.length) {
    return <div className="empty">No pending merges. Resolve conflicts from a chat
      (ctx_resolve) or with <code>ctx merge --continue</code>.</div>;
  }
  return (
    <div>
      {pending.map((m) => (
        <section key={m.merge_id} className="group">
          <h3>Merge {m.from} → {m.branch}</h3>
          {m.conflicts.map((c: any) => (
            <div key={c.conflict_id} className="conflict">
              <div className="muted small">{c.kind.replace("_", " ")}{c.reason && <> · {c.reason}</>}
                {c.resolution && <span className="pill">{c.resolution}</span>}</div>
              <div className="sides">
                <div><span className="side">ours</span>{c.ours?.content ?? "(removed)"}</div>
                <div><span className="side">theirs</span>{c.theirs?.content ?? "(removed)"}</div>
              </div>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

function Members({ repo, canEdit }: { repo: string; canEdit: boolean }) {
  const [rows, error, reload] = useLoad<any[]>(() => api(`/repos/${repo}/members`), [repo]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("viewer");
  const [msg, setMsg] = useState<string>();
  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(undefined);
    try {
      await api(`/repos/${repo}/members`, { method: "PUT", body: JSON.stringify({ email, role }) });
      setEmail("");
      reload();
    } catch (err) {
      setMsg(err instanceof ApiError ? err.message : String(err));
    }
  };
  const remove = async (em: string) => {
    await api(`/repos/${repo}/members/${encodeURIComponent(em)}`, { method: "DELETE" });
    reload();
  };
  if (error) return <div className="error">{error}</div>;
  return (
    <div>
      <ul className="members">
        {rows?.map((r) => (
          <li key={r.id}><span>{r.email}</span><span className="pill">{r.role}</span>
            {canEdit && r.role !== "owner" && <button className="link" onClick={() => remove(r.email)}>Remove</button>}
          </li>
        ))}
      </ul>
      {canEdit && (
        <form className="share" onSubmit={add}>
          <input type="email" required placeholder="teammate@example.com" value={email}
                 onChange={(e) => setEmail(e.target.value)} aria-label="Email" />
          <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role">
            <option>viewer</option><option>editor</option><option>owner</option>
          </select>
          <button type="submit" className="primary">Share</button>
        </form>
      )}
      {msg && <div className="error">{msg}</div>}
      <p className="muted small">People must sign in once before you can share with them.</p>
    </div>
  );
}
