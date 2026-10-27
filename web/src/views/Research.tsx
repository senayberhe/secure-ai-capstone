import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { Client, JobDetail, JobRow, Me } from "../api";
import { Badge, Button, Card, ErrorNote, Field, useAction, tw } from "../ui";

const IN_FLIGHT = new Set(["queued", "running"]);

export default function Research({ client, me }: { client: Client; me: Me }) {
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const list = useAction();
  const refresh = useCallback(() => list.run(() => client.jobs().then(setJobs)), [client, list.run]);
  useEffect(() => { refresh(); }, [refresh]);

  // Poll while the worker still has jobs to finish.
  const waiting = jobs.some((j) => IN_FLIGHT.has(j.status));
  useEffect(() => {
    if (!waiting) return;
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [waiting, refresh]);

  const mine = jobs.filter((j) => j.owner === me.user);
  const toReview = jobs.filter((j) => j.can_review);

  return (
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <div className="grid content-start gap-5">
        <NewJob client={client} me={me} onCreated={(id) => { setSelected(id); refresh(); }} />
        {toReview.length > 0 && (
          <Card title={`Awaiting your review (${toReview.length})`}>
            <JobList jobs={toReview} selected={selected} onSelect={setSelected} showOwner />
          </Card>
        )}
        <Card title="My research jobs" actions={<Button variant="ghost" onClick={refresh}>Refresh</Button>}>
          <ErrorNote error={list.error} />
          {mine.length === 0 ? <p className="text-muted">No jobs yet.</p>
            : <JobList jobs={mine} selected={selected} onSelect={setSelected} />}
          {waiting && <p className="text-[0.8rem] text-muted">Waiting for the worker… (updates every 2s)</p>}
        </Card>
      </div>
      {selected
        ? <JobPanel key={selected} client={client} row={jobs.find((j) => j.job_id === selected)} jobId={selected} onChanged={refresh} />
        : <Card title="Report"><p className="text-muted">Select a job to read its report.</p></Card>}
    </div>
  );
}

function NewJob({ client, me, onCreated }: { client: Client; me: Me; onCreated: (id: string) => void }) {
  const [question, setQuestion] = useState("");
  const [group, setGroup] = useState("");
  const create = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    const job = await create.run(() => client.createJob(question.trim(), group || undefined));
    if (job) {
      setQuestion("");
      onCreated(job.job_id);
    }
  }

  return (
    <Card title="New research job">
      <form className={tw.form} onSubmit={submit}>
        <Field label="Question"><input value={question} maxLength={3000} onChange={(e) => setQuestion(e.target.value)} required /></Field>
        <Field label="Group">
          <select value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">All my groups</option>
            {me.groups.map((g) => <option key={g}>{g}</option>)}
          </select>
        </Field>
        <ErrorNote error={create.error} />
        <Button variant="primary" disabled={create.busy || !question.trim()}>{create.busy ? "Submitting…" : "Submit"}</Button>
      </form>
    </Card>
  );
}

function JobList({ jobs, selected, onSelect, showOwner }: {
  jobs: JobRow[]; selected: string | null; onSelect: (id: string) => void; showOwner?: boolean;
}) {
  return (
    <ul className={tw.rows}>
      {jobs.map((j) => (
        <li key={j.job_id}>
          <Button variant="plain" className={`grid w-full gap-1.5 bg-surface text-left ${selected === j.job_id
                    ? "border-accent ring-1 ring-accent" : "border-line hover:border-muted"}`} onClick={() => onSelect(j.job_id)}>
            <span className="wrap-anywhere">{j.question}</span>
            <span className={tw.rowMeta}>
              <Badge value={j.status} />
              <span className="text-muted">{showOwner ? `${j.owner} · ` : ""}{new Date(j.created_at).toLocaleString()}</span>
            </span>
          </Button>
        </li>
      ))}
    </ul>
  );
}

function JobPanel({ client, row, jobId, onChanged }: {
  client: Client; row: JobRow | undefined; jobId: string; onChanged: () => void;
}) {
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const load = useAction();
  const decide = useAction();
  const status = row?.status;

  useEffect(() => { load.run(() => client.job(jobId).then(setDetail)); }, [client, jobId, status, load.run]);

  async function review(approved: boolean) {
    const result = await decide.run(() => client.review(jobId, approved));
    if (result) onChanged();
  }

  return (
    <Card title="Report" actions={detail && <Badge value={detail.status} />}>
      <ErrorNote error={load.error} />
      {row && <p className={tw.label}>{row.question}</p>}
      {detail && IN_FLIGHT.has(detail.status) && <p className="text-muted">The worker hasn't finished this job yet.</p>}
      {detail?.status === "failed" && <p className="text-muted">The job failed after its retries. Try rephrasing the question.</p>}
      {/* Rendered as text, never HTML: reports quote untrusted source passages. */}
      {detail?.report && <pre className="my-2 max-h-[60vh] overflow-auto rounded-lg bg-surface-2 p-3.5 font-sans whitespace-pre-wrap wrap-anywhere">{detail.report}</pre>}
      {detail && detail.citations.length > 0 && (
        <p className="text-[0.8rem] text-muted">Cited: {detail.citations.map((c) => <code key={c}>{c}</code>)}</p>
      )}
      {row?.can_review && (
        <div className="mt-3 grid gap-2.5 border-t border-line pt-3">
          <p className="text-[0.8rem] text-muted">You are reviewing {row.owner}'s report. Approval is internal sign-off only; nothing is published.</p>
          <ErrorNote error={decide.error} />
          <div className={tw.actions}>
            <Button variant="primary" disabled={decide.busy} onClick={() => review(true)}>Approve</Button>
            <Button variant="danger" disabled={decide.busy} onClick={() => review(false)}>Reject</Button>
          </div>
        </div>
      )}
    </Card>
  );
}
