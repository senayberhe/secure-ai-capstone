import { useState, type ChangeEvent } from "react";
import type { Client, Interval, Scorecard, ScoreRow } from "../api";
import { Badge, Button, Card, ErrorNote, Field, useAction, tw } from "../ui";

const MAX_ROWS = 100000;

// Plain-language reasons for each release gate the API can fail.
function explain(gate: string): string {
  const [name, group] = gate.split(":");
  const known: Record<string, string> = {
    sample_size: "Fewer than 100 labeled rows",
    small_subgroup: "A group has fewer than 20 rows",
    accuracy: "Overall accuracy below 0.80",
    precision: "Overall precision below 0.80",
    recall: "Overall recall below 0.80",
    precision_unmeasurable: "Precision cannot be measured (no positive predictions)",
    recall_unmeasurable: group ? `Recall for “${group}” cannot be compared (fewer than 20 labeled positives)`
      : "Recall cannot be measured (no labeled positives)",
    group_recall: `Recall for “${group}” is below 0.70, even if the overall numbers pass`,
    selection_gap: "Positive-prediction rates differ between groups by more than 0.20",
    recall_gap: "Recall differs between groups by more than 0.20",
    unsupported_claims: "More than 5% unsupported claims",
    privacy_leak: "At least one privacy leak",
  };
  return known[name] ?? gate;
}

export function parseRows(text: string): ScoreRow[] {
  const trimmed = text.trim();
  if (!trimmed) throw new Error("Paste or load some rows first.");
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
    const data = JSON.parse(trimmed);
    const rows = Array.isArray(data) ? data : data.records;
    if (!Array.isArray(rows)) throw new Error("JSON must be an array of rows or {\"records\": [...]}.");
    return rows;
  }
  const [header, ...lines] = trimmed.split(/\r?\n/).filter((l) => l.trim());
  const cols = header.split(",").map((c) => c.trim().toLowerCase());
  for (const required of ["true", "pred", "group"]) {
    if (!cols.includes(required)) throw new Error(`CSV header must include "${required}".`);
  }
  return lines.map((line, i) => {
    const cells = line.split(",").map((c) => c.trim());
    const get = (name: string) => cells[cols.indexOf(name)];
    const bit = (name: string) => {
      const v = get(name);
      if (v !== "0" && v !== "1") throw new Error(`Row ${i + 2}: "${name}" must be 0 or 1.`);
      return Number(v) as 0 | 1;
    };
    const flag = (name: string) => cols.includes(name) && ["1", "true", "yes"].includes((get(name) ?? "").toLowerCase());
    return { true: bit("true"), pred: bit("pred"), group: get("group"), unsupported: flag("unsupported"), leak: flag("leak") };
  });
}

function sample(kind: "balanced" | "hidden"): string {
  // hidden: group B is a quarter of the data and the model misses 40% of its positives.
  const spec = kind === "balanced"
    ? [["A", 100, 50, 48, 2], ["B", 100, 50, 47, 3]]
    : [["A", 300, 150, 150, 0], ["B", 100, 50, 30, 0]];
  const lines = ["true,pred,group"];
  for (const [group, n, positives, tp, fp] of spec as [string, number, number, number, number][]) {
    for (let i = 0; i < n; i++) {
      const t = i < positives ? 1 : 0;
      const p = i < tp || (i >= positives && i < positives + fp) ? 1 : 0;
      lines.push(`${t},${p},${group}`);
    }
  }
  return lines.join("\n");
}

const pct = (v: number | null) => (v === null ? "n/a" : `${(v * 100).toFixed(1)}%`);
const ci = (v: Interval) => (v ? `${(v[0] * 100).toFixed(0)}–${(v[1] * 100).toFixed(0)}%` : "");

export default function Quality({ client }: { client: Client }) {
  const [text, setText] = useState("");
  const [result, setResult] = useState<Scorecard | null>(null);
  const action = useAction();

  async function run() {
    let rows: ScoreRow[];
    try {
      rows = parseRows(text);
      if (rows.length < 20 || rows.length > MAX_ROWS) throw new Error(`Need 20 to ${MAX_ROWS} rows; got ${rows.length}.`);
    } catch (e) {
      action.setError(e instanceof Error ? e.message : "Could not read the rows.");
      return;
    }
    const r = await action.run(() => client.scorecard(rows));
    if (r) setResult(r);
  }

  function load(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    // Read in the browser only; rows go nowhere except the scorecard request.
    file.text().then(setText);
  }

  return (
    <div className="grid content-start gap-5">
      <Card title="Score a labeled sample">
        <div className={tw.form}>
          <Field label="Rows (CSV with true,pred,group[,unsupported,leak] — or JSON)"
                 hint="Use synthetic or de-identified labels. The API returns aggregates only and stores nothing.">
            <textarea rows={8} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
                      placeholder={"true,pred,group\n1,1,A\n0,0,B\n…"} />
          </Field>
          <div className={tw.actions}>
            <Button variant="primary" disabled={action.busy || !text.trim()} onClick={run}>
              {action.busy ? "Scoring…" : "Run scorecard"}
            </Button>
            <label className="relative cursor-pointer rounded-lg border border-line bg-surface-2 px-3.5 py-1.5 hover:border-muted focus-within:outline-2 focus-within:outline-offset-1 focus-within:outline-accent">Open file…<input className="absolute size-px opacity-0" type="file" accept=".csv,.json,text/csv,application/json" onChange={load} /></label>
            <Button variant="ghost" onClick={() => setText(sample("balanced"))}>Example: healthy model</Button>
            <Button variant="ghost" onClick={() => setText(sample("hidden"))}>Example: hidden subgroup failure</Button>
          </div>
          <ErrorNote error={action.error} />
        </div>
      </Card>
      {result && <Result r={result} />}
    </div>
  );
}

function Result({ r }: { r: Scorecard }) {
  const metrics: [string, number | null, Interval][] = [
    ["Accuracy", r.accuracy, r.accuracy_ci95], ["Precision", r.precision, r.precision_ci95],
    ["Recall", r.recall, r.recall_ci95], ["F1", r.f1, null],
  ];
  const held = r.decision === "HOLD";
  return (
    <>
      <div className={`flex flex-wrap items-baseline gap-3 rounded-xl px-4.5 py-3.5 ${held ? "bg-bad-bg text-bad" : "bg-ok-bg text-ok"}`} role="status">
        <strong>{held ? "Hold" : "Ready for human review"}</strong>
        <span className="text-ink">{held ? `${r.failed_gates.length} gate(s) failed on ${r.n} rows.` : `All gates passed on ${r.n} rows. A person still decides.`}</span>
      </div>
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {metrics.map(([name, value, interval]) => (
          <div className="grid gap-1 rounded-xl border border-line bg-surface p-4 shadow-sm sm:p-5" key={name}>
            <span className={tw.label}>{name}</span>
            <span className="text-3xl font-bold tabular-nums">{pct(value)}</span>
            <span className="text-[0.8rem] text-muted">{interval ? `95% CI ${ci(interval)}` : name === "F1" ? "harmonic mean" : "not measurable"}</span>
          </div>
        ))}
      </div>
      <div className="grid items-start gap-5 lg:grid-cols-2">
        <Card title="By group">
          <div className={tw.tableWrap}>
            <table>
              <thead><tr><th>Group</th><th>Rows</th><th>Positives</th><th>Accuracy</th><th>Precision</th><th>Recall (95% CI)</th><th>Predicted +</th></tr></thead>
              <tbody>
                {Object.entries(r.groups).map(([name, g]) => (
                  <tr key={name}>
                    <td><strong>{name}</strong></td><td>{g.n}</td><td>{g.positives}</td>
                    <td>{pct(g.accuracy)}</td><td>{pct(g.precision)}</td>
                    <td>{pct(g.recall)} <span className="text-[0.8rem] text-muted">{ci(g.recall_ci95)}</span></td>
                    <td>{pct(g.positive_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[0.8rem] text-muted">Selection-rate gap {pct(r.selection_rate_gap)} · recall gap {pct(r.recall_gap)} · unsupported {pct(r.unsupported_rate)} · leaks {r.leaks}</p>
        </Card>
        <Card title="Release gates" actions={<Badge value={held ? "hold" : "eligible"} />}>
          {r.failed_gates.length === 0 ? <p className={tw.success}>No gate failed.</p> : (
            <ul className="m-0 grid list-disc gap-1.5 pl-5">{r.failed_gates.map((g) => <li key={g}>{explain(g)}</li>)}</ul>
          )}
          <p className="text-[0.8rem] text-muted">Thresholds are teaching values. Passing is evidence for a human decision, not a deployment approval.</p>
        </Card>
      </div>
    </>
  );
}
