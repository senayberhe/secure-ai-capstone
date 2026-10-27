import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App, { SignIn, tabsFor } from "./App";
import { createAuth } from "./api";

type Route = (url: string, init?: RequestInit) => [number, unknown];

function serve(route: Route) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const [status, body] = route(url, init);
    return new Response(JSON.stringify(body), { status });
  }));
}

const bearer = (init?: RequestInit) => new Headers(init?.headers).get("Authorization");
const noSession: [number, unknown] = [401, { detail: "Authentication required" }];

async function openTokenForm() {
  await userEvent.click(await screen.findByText("Use a development bearer token"));
}

const alice = { user: "alice", tenant: "acme", role: "editor", groups: ["engineering"], release_operator: false };
const rachel = { user: "rachel", tenant: "acme", role: "reviewer", groups: ["engineering"], release_operator: true };

beforeEach(() => sessionStorage.clear());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("tabsFor", () => {
  it("shows release and incident tabs only to the right roles", () => {
    expect(tabsFor(alice).map((t) => t.id)).toEqual(["knowledge", "research"]);
    expect(tabsFor(rachel).map((t) => t.id)).toEqual(["knowledge", "research", "quality", "releases", "incidents"]);
  });
});

describe("App", () => {
  it("rejects a bad token", async () => {
    serve(() => noSession);
    render(<App />);
    await openTokenForm();
    await userEvent.type(screen.getByLabelText("Bearer token"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Use token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("rejected");
    expect(sessionStorage.getItem("capstone.token")).toBeNull();
  });

  it("signs in, asks a question and renders the answer as text", async () => {
    serve((url, init) => {
      if (url === "/v1/me") return bearer(init) ? [200, alice] : noSession;
      if (url === "/v1/documents") return [200, { documents: [] }];
      if (url === "/v1/rag/query") return [200, { answer: "<img src=x onerror=alert(1)> Receipts within 30 days [d1:c0]",
        citations: [{ citation_id: "d1:c0", document_id: "d1", title: "Travel policy" }], mode: "offline-extractive" }];
      return [404, { detail: "Not Found" }];
    });
    const { container } = render(<App />);
    await openTokenForm();
    await userEvent.type(screen.getByLabelText("Bearer token"), "alice-token");
    await userEvent.click(screen.getByRole("button", { name: "Use token" }));
    expect(await screen.findByText(/alice/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Releases" })).toBeNull();
    await userEvent.type(screen.getByLabelText("Question"), "receipts?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/Receipts within 30 days/)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();  // untrusted text is never parsed as HTML
    expect(screen.getByText("Travel policy")).toBeInTheDocument();
  });

  it("lets an in-scope reviewer read and approve a report", async () => {
    sessionStorage.setItem("capstone.token", "rachel-token");
    let status = "pending_review";
    serve((url, init) => {
      if (url === "/v1/me") return [200, rachel];
      if (url === "/v1/research/jobs") return [200, { jobs: [{ job_id: "j1", owner: "alice", question: "travel receipts",
        status, model_version: null, created_at: new Date().toISOString(), can_review: status === "pending_review" }] }];
      if (url === "/v1/research/jobs/j1") return [200, { job_id: "j1", status, report: "Authorized source extracts", citations: ["d1:c0"], model_version: null }];
      if (url === "/v1/research/jobs/j1/review" && init?.method === "POST") { status = "approved"; return [200, { job_id: "j1", status }]; }
      return [200, { documents: [] }];
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Research" }));
    await userEvent.click(await screen.findByRole("button", { name: /travel receipts/ }));
    expect(await screen.findByText("Authorized source extracts")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
  });

  it("shows no quick sign-in outside `npm run dev`", async () => {
    serve(() => noSession);
    render(<App />);
    await openTokenForm();
    expect(screen.queryByText(/Development only/)).toBeNull();
  });
});

const ada = { user: "ada@example.com", tenant: "ws-1", role: "admin", groups: ["personal"], release_operator: false };

describe("Accounts", () => {
  it("creates an account and uses the session cookie, not a stored token", async () => {
    let signedIn = false;
    serve((url, init) => {
      if (url === "/v1/auth/config") return [200, { password: true, google: false }];
      if (url === "/v1/auth/signup") {
        expect(JSON.parse(init?.body as string)).toEqual({ email: "ada@example.com", password: "correct horse battery" });
        expect(new Headers(init?.headers).get("X-Requested-With")).toBe("capstone");
        signedIn = true;
        return [201, ada];
      }
      if (url === "/v1/me") return signedIn && !bearer(init) ? [200, ada] : noSession;
      return [200, { documents: [] }];
    });
    render(<App />);
    await userEvent.click(await screen.findByRole("tab", { name: "Sign up" }));
    expect(screen.queryByText("Continue with Google")).toBeNull();
    await userEvent.type(screen.getByLabelText(/Email/), "ada@example.com");
    await userEvent.type(screen.getByLabelText(/Password/), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("ada@example.com")).toBeInTheDocument();
    expect(sessionStorage.getItem("capstone.token")).toBeNull();
  });

  it("shows the server's message for a wrong password", async () => {
    serve((url) => (url === "/v1/auth/login" ? [401, { detail: "Invalid email or password" }] : noSession));
    render(<App />);
    await userEvent.type(await screen.findByLabelText(/Email/), "ada@example.com");
    await userEvent.type(screen.getByLabelText(/Password/), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
  });

  it("restores a cookie session and signs out on the server", async () => {
    const calls: string[] = [];
    serve((url) => {
      calls.push(url);
      if (url === "/v1/me") return calls.includes("/v1/auth/logout") ? noSession : [200, ada];
      if (url === "/v1/auth/logout") return [204, null];
      return [200, { documents: [] }];
    });
    render(<App />);
    expect(await screen.findByText("ada@example.com")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Welcome back" })).toBeInTheDocument();
    expect(calls).toContain("/v1/auth/logout");
  });

  it("offers Google when the server has it configured and reports a failed redirect", async () => {
    window.history.replaceState(null, "", "/?auth_error=google");
    serve((url) => (url === "/v1/auth/config" ? [200, { password: true, google: true }] : noSession));
    render(<App />);
    const google = await screen.findByRole("link", { name: "Continue with Google" });
    expect(google).toHaveAttribute("href", "/v1/auth/google/start");
    expect(screen.getByRole("alert")).toHaveTextContent("Google sign-in didn't complete");
    expect(window.location.search).toBe("");
  });
});

describe("SignIn quick identities", () => {
  it("signs in with the chosen development identity", async () => {
    serve((url) => (url === "/v1/me" ? [200, rachel] : [404, {}]));
    const onSignedIn = vi.fn();
    render(<SignIn onSignedIn={onSignedIn} auth={{ ...createAuth(), config: async () => ({ password: true, google: false }) }} identities={[
      { user: "alice", role: "editor", tenant: "acme", token: "alice-token" },
      { user: "rachel", role: "reviewer", tenant: "acme", token: "rachel-token" },
    ]} />);
    await openTokenForm();
    const buttons = screen.getAllByRole("button", { name: /acme/ });
    expect(buttons[0]).toHaveTextContent("rachel");  // reviewers listed before editors
    await userEvent.click(buttons[0]);
    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith("rachel-token", rachel));
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer rachel-token");
  });
});
