import { useCallback, useEffect, useState, type FormEvent } from "react";
import type { Answer, Client, DocumentRow, Me } from "../api";
import { Button, Card, ErrorNote, Field, useAction, tw } from "../ui";

const LABELS: Record<string, string> = {
  EMAIL_ADDRESS: "email", PHONE_NUMBER: "phone", US_SSN: "SSN", CREDIT_CARD: "card number", IBAN_CODE: "IBAN", PERSON: "name",
};

export default function Knowledge({ client, me }: { client: Client; me: Me }) {
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const list = useAction();
  const refresh = useCallback(() => list.run(() => client.documents().then(setDocs)), [client, list.run]);
  useEffect(() => { refresh(); }, [refresh]);
  const canUpload = (me.role === "editor" || me.role === "admin") && me.groups.length > 0;

  return (
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <div className="grid content-start gap-5">
        <AskCard client={client} me={me} />
        {canUpload && <UploadCard client={client} me={me} onUploaded={refresh} />}
      </div>
      <Card title="Documents you can access" actions={<Button variant="ghost" onClick={refresh}>Refresh</Button>}>
        <ErrorNote error={list.error} />
        {me.groups.length === 0 && <p className="text-muted">Your identity has no document groups, so nothing is visible.</p>}
        {docs.length === 0 && me.groups.length > 0 && <p className="text-muted">No documents yet.</p>}
        <ul className={tw.rows}>
          {docs.map((d) => (
            <li className={tw.row} key={d.document_id}>
              <strong>{d.title}</strong>
              <span className="text-muted">{d.group} · {d.owner} · {new Date(d.created_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

function AskCard({ client, me }: { client: Client; me: Me }) {
  const [question, setQuestion] = useState("");
  const [group, setGroup] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const ask = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    const result = await ask.run(() => client.ask(question.trim(), group || undefined));
    if (result) setAnswer(result);
  }

  return (
    <Card title="Ask your documents">
      <form className={tw.form} onSubmit={submit}>
        <Field label="Question">
          <input value={question} maxLength={3000} onChange={(e) => setQuestion(e.target.value)} required
                 placeholder="When should travel receipts be submitted?" />
        </Field>
        <Field label="Group">
          <select value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">All my groups</option>
            {me.groups.map((g) => <option key={g}>{g}</option>)}
          </select>
        </Field>
        <ErrorNote error={ask.error} />
        <Button variant="primary" disabled={ask.busy || !question.trim()}>{ask.busy ? "Searching…" : "Ask"}</Button>
      </form>
      {answer && (
        <div className="mt-4 grid gap-1.5 border-t border-line pt-4">
          <p className={tw.label}>Answer <span className="text-muted">({answer.mode})</span></p>
          {/* Rendered as text, never HTML: source passages are untrusted. */}
          <p className={tw.prewrap}>{answer.answer}</p>
          {answer.citations.length > 0 && (
            <>
              <p className={tw.label}>Sources</p>
              <ul className="m-0 list-disc pl-5">
                {answer.citations.map((c) => <li key={c.citation_id}>{c.title} <code>{c.citation_id}</code></li>)}
              </ul>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function UploadCard({ client, me, onUploaded }: { client: Client; me: Me; onUploaded: () => void }) {
  const [title, setTitle] = useState("");
  const [group, setGroup] = useState(me.groups[0] ?? "");
  const [text, setText] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const upload = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setDone(null);
    const result = await upload.run(() => client.upload(title.trim(), group, text));
    if (result) {
      const redacted = Object.entries(result.redactions ?? {});
      setDone(`Uploaded in ${result.chunks} chunk(s).` + (redacted.length
        ? ` Redacted before storing: ${redacted.map(([kind, n]) => `${n} ${LABELS[kind] ?? kind}`).join(", ")}.`
        : " No personal data detected."));
      setTitle("");
      setText("");
      onUploaded();
    }
  }

  return (
    <Card title="Upload a document">
      <form className={tw.form} onSubmit={submit}>
        <Field label="Title"><input value={title} maxLength={180} onChange={(e) => setTitle(e.target.value)} required /></Field>
        <Field label="Group" hint="Only members of this group (in your tenant) can retrieve it.">
          <select value={group} onChange={(e) => setGroup(e.target.value)}>
            {me.groups.map((g) => <option key={g}>{g}</option>)}
          </select>
        </Field>
        <Field label="Text"><textarea rows={6} value={text} maxLength={25000} onChange={(e) => setText(e.target.value)} required /></Field>
        <ErrorNote error={upload.error} />
        {done && <p className={tw.success}>{done}</p>}
        <Button variant="primary" disabled={upload.busy || !title.trim() || !text.trim()}>{upload.busy ? "Uploading…" : "Upload"}</Button>
      </form>
    </Card>
  );
}
