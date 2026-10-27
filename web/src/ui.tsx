import { useCallback, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { ApiError } from "./api";

/** Shared Tailwind class strings for patterns that repeat across views. */
export const tw = {
  form: "grid gap-3",
  label: "m-0 text-xs font-semibold uppercase tracking-wide text-muted",
  small: "text-[0.8rem]",
  hint: "text-[0.8rem] text-muted",
  actions: "flex flex-wrap items-center gap-2",
  rows: "m-0 grid list-none gap-2 p-0",
  row: "grid gap-1 rounded-lg border border-line px-3 py-2.5 wrap-anywhere",
  rowMeta: "flex flex-wrap items-center gap-2.5 text-sm",
  divider: "mt-4 border-t border-line pt-4",
  success: "m-0 text-ok",
  prewrap: "whitespace-pre-wrap wrap-anywhere",
  tableWrap: "overflow-x-auto",
};

const BUTTON = {
  default: "border-line bg-surface-2 text-ink hover:border-muted",
  primary: "justify-self-start border-accent bg-accent font-semibold text-accent-ink hover:brightness-110",
  danger: "border-bad bg-transparent text-bad hover:bg-bad-bg",
  plain: "",
  ghost: "border-transparent bg-transparent px-2 py-1 text-muted hover:text-ink",
};

export function Button({ variant = "default", className = "", ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: keyof typeof BUTTON }) {
  return (
    <button {...props}
      className={`rounded-lg border px-3.5 py-1.5 transition disabled:cursor-not-allowed disabled:opacity-55 ${BUTTON[variant]} ${className}`} />
  );
}

export function Card({ title, children, actions, className = "" }: {
  title: string; children: ReactNode; actions?: ReactNode; className?: string;
}) {
  return (
    <section className={`min-w-0 rounded-xl border border-line bg-surface p-4 shadow-sm sm:p-5 ${className}`}>
      <header className="mb-3.5 flex items-center justify-between gap-3">
        <h2>{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="grid gap-1">
      <span className={tw.label}>{label}</span>
      {children}
      {hint && <span className={tw.hint}>{hint}</span>}
    </label>
  );
}

const TONE: Record<string, string> = {
  approved: "ok", active: "ok", resolved: "ok", eligible: "ok", continue: "ok",
  pending_review: "warn", canary: "warn", contained: "warn", queued: "info", running: "info", registered: "info",
  rejected: "bad", failed: "bad", rolled_back: "bad", open: "bad", hold: "bad", retired: "muted",
  matched: "ok", mismatch: "bad",
};

const TONE_CLASS: Record<string, string> = {
  ok: "bg-ok-bg text-ok", warn: "bg-warn-bg text-warn", bad: "bg-bad-bg text-bad",
  info: "bg-info-bg text-info", muted: "bg-surface-2 text-muted",
};

export function Badge({ value }: { value: string }) {
  return (
    <span className={`inline-block whitespace-nowrap rounded-full px-2 py-px text-xs font-semibold capitalize ${TONE_CLASS[TONE[value] ?? "muted"]}`}>
      {value.replace(/_/g, " ")}
    </span>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <p className="m-0 rounded-lg bg-bad-bg px-3 py-2 text-bad" role="alert">{error}</p> : null;
}

export function describe(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Your token was rejected. Sign out and sign in again.";
    if (error.status === 429) return "Rate limit reached. Wait a minute and try again.";
    return error.message;
  }
  return "Could not reach the API. Is the server running?";
}

/** Runs an async action with a busy flag and a user-readable error. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(describe(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, setError };
}
