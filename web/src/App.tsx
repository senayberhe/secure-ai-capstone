import { useEffect, useMemo, useState, type FormEvent } from "react";
import { createClient, type Client, type Me } from "./api";
import { Button, describe, ErrorNote, Field } from "./ui";
import Knowledge from "./views/Knowledge";
import Research from "./views/Research";
import Releases from "./views/Releases";
import Incidents from "./views/Incidents";
import Quality from "./views/Quality";

// sessionStorage: the token survives a reload but is dropped when the tab closes.
// Development bearer keys only; a real deployment should sign in through an identity provider.
const TOKEN_KEY = "capstone.token";

function readToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: stay signed in for this page only */
  }
}

function Logo() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="size-5 text-accent" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z" /><path d="M9 12l2 2 4-4" />
    </svg>
  );
}

type Tab = "knowledge" | "research" | "quality" | "releases" | "incidents";

export function tabsFor(me: Me): { id: Tab; label: string }[] {
  const tabs: { id: Tab; label: string }[] = [
    { id: "knowledge", label: "Knowledge" },
    { id: "research", label: "Research" },
  ];
  if (me.role === "admin" || me.role === "reviewer") tabs.push({ id: "quality", label: "Quality" });
  if (me.release_operator) tabs.push({ id: "releases", label: "Releases" });
  if (me.role === "admin" || me.role === "reviewer") tabs.push({ id: "incidents", label: "Incidents" });
  return tabs;
}

const ROLE_ORDER = ["admin", "reviewer", "editor", "reader"];

export function SignIn({ onSignedIn, identities = __DEV_IDENTITIES__ }: {
  onSignedIn: (token: string, me: Me) => void; identities?: DevIdentity[];
}) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const quick = [...identities].sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role));

  async function signIn(value: string) {
    setBusy(true);
    setError(null);
    try {
      onSignedIn(value, await createClient(value).me());
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    signIn(token.trim());
  }

  return (
    <main className="grid min-h-screen place-items-center p-4">
      <form className="grid w-full max-w-md gap-3.5 rounded-2xl border border-line bg-surface p-6 shadow-lg sm:p-8" onSubmit={submit}>
        <div className="flex items-center gap-2">
          <Logo />
          <p className="m-0 text-xs font-bold uppercase tracking-[0.12em] text-accent">Secure AI Platform</p>
        </div>
        <h1>Sign in</h1>
        <p className="m-0 text-muted">Paste a bearer token from <code>CAPSTONE_TOKENS_JSON</code> in your <code>.env</code>.</p>
        <Field label="Bearer token">
          <input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} required />
        </Field>
        <ErrorNote error={error} />
        <Button variant="primary" className="w-full" disabled={busy || !token.trim()}>{busy ? "Checking…" : "Sign in"}</Button>
        {quick.length > 0 && (
          <div className="mt-2 grid gap-2 border-t border-dashed border-line pt-4">
            <p className="m-0 text-xs font-semibold uppercase tracking-wide text-warn">Development only · sign in as</p>
            <div className="grid gap-2 sm:grid-cols-2">
              {quick.map((id) => (
                <Button key={id.token} type="button" disabled={busy} onClick={() => signIn(id.token)}
                        className="grid gap-0.5 text-left">
                  <span className="font-semibold">{id.user}</span>
                  <span className="text-xs text-muted">{id.role} · {id.tenant}</span>
                </Button>
              ))}
            </div>
            <p className="m-0 text-xs text-muted">Read from <code>.env</code> by <code>npm run dev</code>. Never shown in a production build.</p>
          </div>
        )}
      </form>
    </main>
  );
}

export default function App() {
  const [token, setToken] = useState(readToken);
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<Tab>("knowledge");
  const [restoring, setRestoring] = useState(Boolean(token));
  const client: Client | null = useMemo(() => (token ? createClient(token) : null), [token]);

  useEffect(() => {
    if (!client || me) return;
    client.me().then(setMe).catch(() => signOut()).finally(() => setRestoring(false));
  }, [client, me]);

  function signOut() {
    writeToken(null);
    setToken("");
    setMe(null);
    setTab("knowledge");
  }

  if (restoring) return <main className="grid min-h-screen place-items-center p-4"><p className="animate-pulse text-muted">Restoring session…</p></main>;
  if (!client || !me) {
    return <SignIn onSignedIn={(t, who) => { writeToken(t); setToken(t); setMe(who); }} />;
  }

  const tabs = tabsFor(me);
  const active = tabs.some((t) => t.id === tab) ? tab : "knowledge";
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2.5 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-2 font-bold"><Logo />Secure AI Platform</div>
          <nav aria-label="Sections" className="order-last -mx-1 flex w-full gap-1 overflow-x-auto md:order-none md:mx-0 md:w-auto md:flex-1">
            {tabs.map((t) => (
              <button key={t.id} aria-current={active === t.id} onClick={() => setTab(t.id)}
                      className={`shrink-0 rounded-lg px-3.5 py-1.5 transition ${active === t.id
                        ? "bg-surface-2 font-semibold text-ink" : "text-muted hover:text-ink"}`}>{t.label}</button>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm text-muted">
            <span><strong className="text-ink">{me.user}</strong> · {me.tenant} · {me.role}</span>
            <Button variant="ghost" onClick={signOut}>Sign out</Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl p-4 sm:p-6">
        {active === "knowledge" && <Knowledge client={client} me={me} />}
        {active === "research" && <Research client={client} me={me} />}
        {active === "quality" && <Quality client={client} />}
        {active === "releases" && <Releases client={client} me={me} />}
        {active === "incidents" && <Incidents client={client} me={me} />}
      </main>
    </div>
  );
}
