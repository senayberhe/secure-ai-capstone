import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { Client, EvidenceEnvelope, EvidenceHistory, EvidenceReceipt, Me, ModelRow } from "../api";
import { Badge, Button, Card, ErrorNote, Field, useAction, tw } from "../ui";

export default function Releases({ client, me }: { client: Client; me: Me }) {
  const [models, setModels] = useState<ModelRow[]>([]);
  const [assigned, setAssigned] = useState<string | null>(null);
  const [lastEvaluation, setLastEvaluation] = useState<{ id: string; version: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const list = useAction();
  const act = useAction();
  const refresh = useCallback(() => list.run(async () => {
    setModels(await client.models());
    setAssigned((await client.assignment()).version);
  }), [client, list.run]);
  useEffect(() => { refresh(); }, [refresh]);

  const hasBaseline = models.some((m) => m.status === "active");
  const versions = models.map((m) => m.version);

  async function rowAction(fn: () => Promise<unknown>) {
    if (await act.run(fn)) refresh();
  }

  return (
    <div className="grid content-start gap-5">
      <Card title="Model registry" actions={<Button variant="ghost" onClick={refresh}>Refresh</Button>}>
        <p className="text-[0.8rem] text-muted">You are routed to <strong>{assigned ?? "no model yet"}</strong>. Releases need an independent reviewer: the registrar cannot canary or promote their own version.</p>
        <ErrorNote error={list.error ?? act.error} />
        {models.length === 0 ? <p className="text-muted">No model versions registered.</p> : (
          <div className={tw.tableWrap}>
            <table>
              <thead>
                <tr><th>Version</th><th>Status</th><th>Traffic</th><th>Provider model</th><th>Canary health</th><th>Registered / reviewed</th><th /></tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.version}>
                    <td><code>{m.version}</code></td>
                    <td><Badge value={m.status} /></td>
                    <td>{m.traffic_pct}%</td>
                    <td>{m.provider_model ?? <span className="text-muted">—</span>}</td>
                    <td>{m.observed_requests
                      ? `${m.observed_errors}/${m.observed_requests} errors${m.observed_critical ? `, ${m.observed_critical} critical` : ""}`
                      : <span className="text-muted">—</span>}</td>
                    <td className="text-muted">{m.registered_by} / {m.reviewed_by ?? "—"}</td>
                    <td><div className={tw.actions}>
                      <Button variant="ghost" onClick={() => setOpen(open === m.version ? null : m.version)}
                              aria-expanded={open === m.version}>{open === m.version ? "Hide" : "History"}</Button>
                      {m.status === "canary" && <>
                        <Button disabled={act.busy} onClick={() => rowAction(() => client.promote(m.version))}>Promote</Button>
                        <Button variant="danger" disabled={act.busy} onClick={() => rowAction(() => client.rollback(m.version))}>Roll back</Button>
                      </>}
                    </div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {open && <History key={open} client={client} version={open} />}
      </Card>
      <div className="grid items-start gap-5 lg:grid-cols-3">
        <Register client={client} onDone={refresh} canRegister={me.role === "admin"} />
        <Evidence client={client} onDone={(id, version) => { setLastEvaluation({ id, version }); refresh(); }} />
        <Rollout client={client} versions={versions} hasBaseline={hasBaseline} evaluation={lastEvaluation} onDone={refresh} />
      </div>
    </div>
  );
}

function Register({ client, onDone, canRegister }: { client: Client; onDone: () => void; canRegister: boolean }) {
  const [version, setVersion] = useState("");
  const [sha, setSha] = useState("");
  const [artifactPath, setArtifactPath] = useState("");
  const [provider, setProvider] = useState("");
  const action = useAction();
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (await action.run(() => client.register(version.trim(), sha.trim().toLowerCase(), artifactPath.trim(), provider.trim() || undefined))) {
      setVersion(""); setSha(""); setArtifactPath(""); setProvider(""); onDone();
    }
  }
  return (
    <Card title="1 · Register">
      {!canRegister ? <p className="text-muted">Only admins register model versions.</p> : (
        <form className={tw.form} onSubmit={submit}>
          <Field label="Version"><input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="candidate-v2" required /></Field>
          <Field label="Artifact SHA-256" hint="64 hex characters. CI and the reviewer's re-hash must both match it.">
            <input value={sha} onChange={(e) => setSha(e.target.value)} pattern="[0-9a-fA-F]{64}" required />
          </Field>
          <Field label="Artifact path" hint="Relative to the server's approved artifact store.">
            <input value={artifactPath} onChange={(e) => setArtifactPath(e.target.value)} placeholder="candidate-v2.safetensors" required />
          </Field>
          <Field label="Provider model (optional)"><input value={provider} onChange={(e) => setProvider(e.target.value)} placeholder="gpt-4.1-mini" /></Field>
          <ErrorNote error={action.error} />
          <Button variant="primary" disabled={action.busy}>Register</Button>
        </form>
      )}
    </Card>
  );
}

function Evidence({ client, onDone }: { client: Client; onDone: (id: string, version: string) => void }) {
  const [text, setText] = useState("");
  const [receipt, setReceipt] = useState<EvidenceReceipt | null>(null);
  const action = useAction();
  async function submit(e: FormEvent) {
    e.preventDefault();
    let envelope: EvidenceEnvelope;
    try {
      envelope = JSON.parse(text);
    } catch {
      action.setError("That is not valid JSON. Paste the evidence.json produced by CI.");
      return;
    }
    const r = await action.run(() => client.submitEvidence(envelope));
    if (r) { setReceipt(r); setText(""); onDone(r.evidence_id, r.version); }
  }
  return (
    <Card title="2 · Attested evidence">
      <form className={tw.form} onSubmit={submit}>
        <Field label="Signed evidence (evidence.json from CI)"
               hint="The server checks the signature, version, artifact digest and age. Typed-in metrics are not accepted.">
          <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} required />
        </Field>
        <ErrorNote error={action.error} />
        {receipt && (
          <p className={tw.small}>
            <Badge value={receipt.decision} /> {receipt.version} attested by <strong>{receipt.attester}</strong>
            <br /><span className="text-muted">digest <code>{receipt.artifact_sha256.slice(0, 16)}…</code></span>
          </p>
        )}
        <Button variant="primary" disabled={action.busy || !text.trim()}>Verify & submit</Button>
      </form>
    </Card>
  );
}

function Rollout({ client, versions, hasBaseline, evaluation, onDone }: {
  client: Client; versions: string[]; hasBaseline: boolean; evaluation: { id: string; version: string } | null; onDone: () => void;
}) {
  const [version, setVersion] = useState("");
  const [evaluationId, setEvaluationId] = useState("");
  const [percent, setPercent] = useState("10");
  const [obs, setObs] = useState({ requests: "100", errors: "0", critical: "0" });
  const [note, setNote] = useState<string | null>(null);
  const action = useAction();

  useEffect(() => {
    if (evaluation) { setVersion(evaluation.version); setEvaluationId(evaluation.id); }
  }, [evaluation]);

  async function start(e: FormEvent) {
    e.preventDefault();
    const r = await action.run(() => hasBaseline
      ? client.canary(version, evaluationId.trim(), Number(percent))
      : client.bootstrap(version, evaluationId.trim()));
    if (r) { setNote(`${r.version} is now ${r.status}.`); onDone(); }
  }
  async function observe() {
    const r = await action.run(() => client.observe(version, Number(obs.requests), Number(obs.errors), Number(obs.critical)));
    if (r) { setNote(`Observation: ${r.decision} (error rate ${(r.error_rate * 100).toFixed(1)}%).`); onDone(); }
  }

  return (
    <Card title={hasBaseline ? "3 · Canary & monitor" : "3 · Activate first baseline"}>
      <form className={tw.form} onSubmit={start}>
        <Field label="Version"><VersionSelect versions={versions} value={version} onChange={(e) => setVersion(e.target.value)} /></Field>
        <Field label="Evidence ID" hint="Filled in from the evidence you just verified. Starting also re-hashes the artifact bytes."><input value={evaluationId} onChange={(e) => setEvaluationId(e.target.value)} required /></Field>
        {hasBaseline && (
          <Field label="Traffic">
            <select value={percent} onChange={(e) => setPercent(e.target.value)}>
              {[5, 10, 25, 50].map((p) => <option key={p} value={p}>{p}%</option>)}
            </select>
          </Field>
        )}
        <Button variant="primary" disabled={action.busy || !version}>{hasBaseline ? "Start canary" : "Activate baseline"}</Button>
      </form>
      {hasBaseline && (
        <div className={`${tw.form} ${tw.divider}`}>
          <p className={tw.label}>Report canary telemetry</p>
          <div className="grid grid-cols-3 gap-2.5">
            <Field label="Requests"><input type="number" min={100} value={obs.requests} onChange={(e) => setObs({ ...obs, requests: e.target.value })} /></Field>
            <Field label="Errors"><input type="number" min={0} value={obs.errors} onChange={(e) => setObs({ ...obs, errors: e.target.value })} /></Field>
            <Field label="Critical"><input type="number" min={0} value={obs.critical} onChange={(e) => setObs({ ...obs, critical: e.target.value })} /></Field>
          </div>
          <p className="text-[0.8rem] text-muted">Rolls back above 5% errors or on any critical event. Simulated input: production should use a trusted collector.</p>
          <Button disabled={action.busy || !version} onClick={observe}>Submit observation</Button>
        </div>
      )}
      <ErrorNote error={action.error} />
      {note && <p className="m-0 text-[0.8rem] text-ok">{note}</p>}
    </Card>
  );
}

function VersionSelect({ versions, value, onChange }: { versions: string[]; value: string; onChange: (e: { target: { value: string } }) => void }) {
  return (
    <select value={value} onChange={onChange} required>
      <option value="">Choose…</option>
      {versions.map((v) => <option key={v}>{v}</option>)}
    </select>
  );
}

function History({ client, version }: { client: Client; version: string }) {
  const [data, setData] = useState<EvidenceHistory | null>(null);
  const load = useAction();
  useEffect(() => { load.run(() => client.evidenceHistory(version).then(setData)); }, [client, version, load.run]);
  const short = (hash: string) => `${hash.slice(0, 12)}…`;
  return (
    <div className="mt-4 grid gap-2.5 border-t border-line pt-4">
      <p className={tw.label}>History for <code>{version}</code></p>
      <ErrorNote error={load.error} />
      {data && (
        <div className="grid items-start gap-5 lg:grid-cols-2">
          <div>
            <p className={tw.label}>Signed evidence</p>
            {data.evidence.length === 0 ? <p className="text-[0.8rem] text-muted">None submitted.</p> : (
              <ul className={tw.rows}>{data.evidence.map((e) => (
                <li className={tw.row} key={e.evidence_id}>
                  <span className={tw.rowMeta}><Badge value={e.decision} /><span>attested by <strong>{e.attester}</strong></span></span>
                  <span className="text-[0.8rem] text-muted">{new Date(e.attested_at).toLocaleString()} · key {e.key_id} · artifact <code>{short(e.artifact_sha256)}</code></span>
                </li>))}
              </ul>
            )}
          </div>
          <div>
            <p className={tw.label}>Artifact re-hash at release</p>
            {data.artifact_verifications.length === 0 ? <p className="text-[0.8rem] text-muted">Not verified yet. It runs when a canary or baseline starts.</p> : (
              <ul className={tw.rows}>{data.artifact_verifications.map((v) => (
                <li className={tw.row} key={v.verified_at + v.sha256}>
                  <span className={tw.rowMeta}><Badge value={v.matched ? "matched" : "mismatch"} /><span>by <strong>{v.verified_by}</strong></span></span>
                  <span className="text-[0.8rem] text-muted">{new Date(v.verified_at).toLocaleString()} · <code>{short(v.sha256)}</code></span>
                </li>))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
