import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError, createAuth, createClient, type Client, type Me } from "./api";
import { Button, describe, ErrorNote, Field } from "./ui";
import Knowledge from "./views/Knowledge";
import Research from "./views/Research";
import Releases from "./views/Releases";
import Incidents from "./views/Incidents";
import Quality from "./views/Quality";

// Accounts use an HttpOnly session cookie the page can't read. Development bearer keys are kept in
// sessionStorage instead: they survive a reload but are dropped when the tab closes.
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

type Mode = "signin" | "signup";

function GoogleMark() {
  return (
    <svg viewBox="0 0 48 48" aria-hidden="true" className="size-[18px]">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.6 13.3l7.9 6.1C12.4 13.7 17.7 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.8c4.3-4 6.9-9.9 6.9-17.2z" />
      <path fill="#FBBC05" d="M10.5 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.1C.9 16.6 0 20.2 0 24s.9 7.4 2.6 10.7l7.9-6.1z" />
      <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.8c-2.1 1.4-4.8 2.2-8.5 2.2-6.3 0-11.6-4.2-13.5-10l-7.9 6.1C6.6 42.6 14.6 48 24 48z" />
    </svg>
  );
}

/** Reads and removes ?auth_error= left by a failed Google redirect, so a reload doesn't repeat it. */
function takeRedirectError(): string | null {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("auth_error")) return null;
  params.delete("auth_error");
  const query = params.toString();
  window.history.replaceState(null, "", window.location.pathname + (query ? `?${query}` : ""));
  return "Google sign-in didn't complete. Try again, or use your email and password.";
}

function authError(err: unknown): string {
  // Sign-in failures (401 wrong password, 409 email taken, 429 too many tries) carry a readable detail.
  return err instanceof ApiError && [401, 409, 422, 429].includes(err.status) ? err.message : describe(err);
}

export function SignIn({ onSignedIn, identities = __DEV_IDENTITIES__, auth = createAuth() }: {
  /** token is a development bearer key, or null for an account session held in the HttpOnly cookie. */
  onSignedIn: (token: string | null, me: Me) => void; identities?: DevIdentity[]; auth?: ReturnType<typeof createAuth>;
}) {
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [google, setGoogle] = useState(false);
  const [error, setError] = useState<string | null>(takeRedirectError);
  const [busy, setBusy] = useState(false);
  const quick = [...identities].sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role));

  useEffect(() => {
    auth.config().then((c) => setGoogle(c.google)).catch(() => setGoogle(false));
  }, [auth]);

  async function attempt(fn: () => Promise<void>, explain: (err: unknown) => string = authError) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  function submitAccount(e: FormEvent) {
    e.preventDefault();
    attempt(async () => {
      const who = mode === "signup" ? await auth.signup(email.trim(), password) : await auth.login(email.trim(), password);
      onSignedIn(null, who);
    });
  }

  function signInWithToken(value: string) {
    attempt(async () => onSignedIn(value, await createClient(value).me()), describe);
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  const signup = mode === "signup";
  return (
    <main className="grid min-h-screen place-items-center p-4">
      <div className="grid w-full max-w-md gap-4 rounded-2xl border border-line bg-surface p-6 shadow-lg sm:p-8">
        <div className="flex items-center gap-2">
          <Logo />
          <p className="m-0 text-xs font-bold uppercase tracking-[0.12em] text-accent">Secure AI Platform</p>
        </div>
        <div>
          <h1>{signup ? "Create your account" : "Welcome back"}</h1>
          <p className="m-0 mt-1 text-muted">
            {signup ? "You get a private workspace. Nobody else can see your documents." : "Sign in to your workspace."}
          </p>
        </div>

        <div role="tablist" aria-label="Account" className="grid grid-cols-2 gap-1 rounded-lg bg-surface-2 p-1">
          {(["signin", "signup"] as const).map((m) => (
            <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => switchMode(m)}
                    className={`rounded-md px-3 py-1.5 text-sm transition ${mode === m
                      ? "bg-surface font-semibold text-ink shadow-sm" : "text-muted hover:text-ink"}`}>
              {m === "signin" ? "Sign in" : "Sign up"}
            </button>
          ))}
        </div>

        {google && (
          <>
            {/* A full-page navigation: the server runs the Google redirect, so no Google script loads here. */}
            <a href="/v1/auth/google/start"
               className="flex items-center justify-center gap-2.5 rounded-lg border border-line bg-surface px-3.5 py-2 font-medium text-ink no-underline transition hover:border-muted">
              <GoogleMark />Continue with Google
            </a>
            <div className="flex items-center gap-3 text-xs uppercase tracking-wide text-muted">
              <span className="h-px flex-1 bg-line" />or<span className="h-px flex-1 bg-line" />
            </div>
          </>
        )}

        <form className="grid gap-3.5" onSubmit={submitAccount}>
          <Field label="Email">
            <input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)}
                   maxLength={64} required />
          </Field>
          <Field label="Password" hint={signup ? "At least 12 characters. A short phrase is easiest to remember." : undefined}>
            <input type="password" autoComplete={signup ? "new-password" : "current-password"} value={password}
                   onChange={(e) => setPassword(e.target.value)} minLength={signup ? 12 : 1} maxLength={128} required />
          </Field>
          <ErrorNote error={error} />
          <Button variant="primary" className="w-full" disabled={busy || !email.trim() || !password}>
            {busy ? "Please wait…" : signup ? "Create account" : "Sign in"}
          </Button>
        </form>

        <details className="border-t border-dashed border-line pt-3 text-sm">
          <summary className="cursor-pointer text-muted hover:text-ink">Use a development bearer token</summary>
          <form className="mt-3 grid gap-3" onSubmit={(e) => { e.preventDefault(); signInWithToken(token.trim()); }}>
            <p className="m-0 text-muted">Paste a token from <code>CAPSTONE_TOKENS_JSON</code> in your <code>.env</code>.</p>
            <Field label="Bearer token">
              <input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} required />
            </Field>
            <Button disabled={busy || !token.trim()}>Use token</Button>
            {quick.length > 0 && (
              <div className="grid gap-2">
                <p className="m-0 text-xs font-semibold uppercase tracking-wide text-warn">Development only · sign in as</p>
                <div className="grid gap-2 sm:grid-cols-2">
                  {quick.map((id) => (
                    <Button key={id.token} type="button" disabled={busy} onClick={() => signInWithToken(id.token)}
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
        </details>
      </div>
    </main>
  );
}

export default function App() {
  // null token = account session cookie. `me` is null until a session is confirmed.
  const [token, setToken] = useState<string | null>(() => readToken() || null);
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<Tab>("knowledge");
  const [restoring, setRestoring] = useState(true);
  const client: Client = useMemo(() => createClient(token), [token]);

  useEffect(() => {
    if (!restoring) return;
    // A stored dev token, or else the session cookie (if any). Either failing just shows the sign-in page.
    client.me().then(setMe).catch(() => { writeToken(null); setToken(null); }).finally(() => setRestoring(false));
  }, [client, restoring]);

  function signOut() {
    if (!token) createAuth().logout().catch(() => { /* the cookie expires on its own */ });
    writeToken(null);
    setToken(null);
    setMe(null);
    setTab("knowledge");
  }

  if (restoring) return <main className="grid min-h-screen place-items-center p-4"><p className="animate-pulse text-muted">Restoring session…</p></main>;
  if (!me) {
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
