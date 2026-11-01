import type { Branch, Commit } from "./api";

const ROW = 34;
const LANE = 16;
const PAD = 14;
const COLORS = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)"];

type Laid = { commit: Commit; lane: number; row: number };

// Lane layout, newest first: each lane waits for the next commit it expects (a parent of a
// commit already placed); merges open extra lanes for their other parents.
function layout(commits: Commit[]): { rows: Laid[]; lanes: number } {
  const lanes: (string | null)[] = [];
  const rows: Laid[] = [];
  commits.forEach((c, row) => {
    let lane = lanes.indexOf(c.id);
    if (lane === -1) {
      lane = lanes.indexOf(null);
      if (lane === -1) lane = lanes.push(null) - 1;
    }
    for (let i = 0; i < lanes.length; i++) if (i !== lane && lanes[i] === c.id) lanes[i] = null;
    lanes[lane] = c.parents[0] ?? null;
    for (const p of c.parents.slice(1)) {
      if (!lanes.includes(p)) {
        const free = lanes.indexOf(null);
        if (free === -1) lanes.push(p); else lanes[free] = p;
      }
    }
    rows.push({ commit: c, lane, row });
  });
  return { rows, lanes: Math.max(1, ...rows.map((r) => r.lane + 1)) };
}

export function Graph(props: {
  commits: Commit[]; branches: Branch[]; selected?: string; onSelect: (id: string) => void;
}) {
  const { rows, lanes } = layout(props.commits);
  const pos = new Map(rows.map((r) => [r.commit.id, r]));
  const width = PAD * 2 + lanes * LANE;
  const heads = new Map<string, string[]>();
  for (const b of props.branches) heads.set(b.head_commit_id, [...(heads.get(b.head_commit_id) || []), b.name]);
  const x = (lane: number) => PAD + lane * LANE;
  const y = (row: number) => ROW / 2 + row * ROW;

  return (
    <div className="graph" role="list" aria-label="Commit history">
      <svg className="graph-lines" width={width} height={rows.length * ROW} aria-hidden="true">
        {rows.flatMap((r) =>
          r.commit.parents.map((p) => {
            const t = pos.get(p);
            const toRow = t ? t.row : rows.length;
            const toLane = t ? t.lane : r.lane;
            const x1 = x(r.lane), y1 = y(r.row), x2 = x(toLane), y2 = y(toRow);
            const d = x1 === x2 ? `M${x1},${y1} L${x2},${y2}`
              : `M${x1},${y1} C${x1},${y1 + ROW * 0.8} ${x2},${y1 + ROW * 0.2} ${x2},${y1 + ROW} L${x2},${y2}`;
            return <path key={r.commit.id + p} d={d} stroke={COLORS[toLane % COLORS.length]}
                         strokeWidth={2} fill="none" />;
          }),
        )}
        {rows.map((r) => (
          <circle key={r.commit.id} cx={x(r.lane)} cy={y(r.row)} r={r.commit.parents.length > 1 ? 6 : 5}
                  fill={r.commit.kind === "merge" ? "var(--card)" : COLORS[r.lane % COLORS.length]}
                  stroke={COLORS[r.lane % COLORS.length]} strokeWidth={2} />
        ))}
      </svg>
      {rows.map((r) => (
        <button key={r.commit.id} role="listitem"
                className={"graph-row" + (props.selected === r.commit.id ? " selected" : "")}
                style={{ height: ROW, paddingLeft: width + 4 }}
                onClick={() => props.onSelect(r.commit.id)}>
          <code className="sha">{r.commit.id.replace(/-/g, "").slice(0, 8)}</code>
          {(heads.get(r.commit.id) || []).map((h) => <span key={h} className="ref">{h}</span>)}
          <span className="msg">{r.commit.message}</span>
          <span className="meta">
            {[r.commit.source_client, r.commit.source_model].filter(Boolean).join(" · ")}
          </span>
        </button>
      ))}
    </div>
  );
}
