import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { Client, IncidentRow, Me } from "../api";
import { Badge, Button, Card, ErrorNote, Field, useAction, tw } from "../ui";

export default function Incidents({ client, me }: { client: Client; me: Me }) {
  const [rows, setRows] = useState<IncidentRow[]>([]);
  const [category, setCategory] = useState("");
  const [summary, setSummary] = useState("");
  const list = useAction();
  const act = useAction();
  const refresh = useCallback(() => list.run(() => client.incidents().then(setRows)), [client, list.run]);
  useEffect(() => { refresh(); }, [refresh]);

  async function open(e: FormEvent) {
    e.preventDefault();
    if (await act.run(() => client.openIncident(category.trim(), summary.trim()))) {
      setCategory(""); setSummary(""); refresh();
    }
  }
  async function step(fn: () => Promise<unknown>) {
    if (await act.run(fn)) refresh();
  }

  return (
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <Card title="Open an incident">
        <form className={tw.form} onSubmit={open}>
          <Field label="Category" hint="Lowercase letters and underscores, e.g. access_violation.">
            <input value={category} pattern="[a-z_]{3,60}" onChange={(e) => setCategory(e.target.value)} required />
          </Field>
          <Field label="Summary" hint="Short operator description. Never paste user prompts or private data.">
            <textarea rows={3} maxLength={300} value={summary} onChange={(e) => setSummary(e.target.value)} required />
          </Field>
          <Button variant="primary" disabled={act.busy}>Open incident</Button>
        </form>
      </Card>
      <Card title="Incidents" actions={<Button variant="ghost" onClick={refresh}>Refresh</Button>}>
        <ErrorNote error={list.error ?? act.error} />
        {rows.length === 0 && <p className="text-muted">No incidents.</p>}
        <ul className={tw.rows}>
          {rows.map((i) => (
            <li className={tw.row} key={i.incident_id}>
              <span className={tw.rowMeta}><Badge value={i.status} /><strong>{i.category}</strong></span>
              <span>{i.summary}</span>
              <span className="text-[0.8rem] text-muted">opened by {i.opened_by}</span>
              <span className={tw.actions}>
                {i.status === "open" && <Button disabled={act.busy} onClick={() => step(() => client.contain(i.incident_id))}>Contain</Button>}
                {i.status === "contained" && me.role === "admin" &&
                  <Button disabled={act.busy} onClick={() => step(() => client.resolve(i.incident_id))}>Resolve</Button>}
                {i.status === "contained" && me.role !== "admin" && <span className="text-[0.8rem] text-muted">An admin resolves it.</span>}
              </span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
