import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App, { SignIn, tabsFor } from "./App";

type Route = (url: string, init?: RequestInit) => [number, unknown];

function serve(route: Route) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const [status, body] = route(url, init);
    return new Response(JSON.stringify(body), { status });
  }));
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
    serve(() => [401, { detail: "Authentication required" }]);
    render(<App />);
    await userEvent.type(screen.getByLabelText("Bearer token"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("token was rejected");
    expect(sessionStorage.getItem("capstone.token")).toBeNull();
  });

  it("signs in, asks a question and renders the answer as text", async () => {
    serve((url) => {
      if (url === "/v1/me") return [200, alice];
      if (url === "/v1/documents") return [200, { documents: [] }];
      if (url === "/v1/rag/query") return [200, { answer: "<img src=x onerror=alert(1)> Receipts within 30 days [d1:c0]",
        citations: [{ citation_id: "d1:c0", document_id: "d1", title: "Travel policy" }], mode: "offline-extractive" }];
      return [404, { detail: "Not Found" }];
    });
    const { container } = render(<App />);
    await userEvent.type(screen.getByLabelText("Bearer token"), "alice-token");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
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

  it("shows no quick sign-in outside `npm run dev`", () => {
    render(<App />);
    expect(screen.queryByText(/Development only/)).toBeNull();
  });
});

describe("SignIn quick identities", () => {
  it("signs in with the chosen development identity", async () => {
    serve((url) => (url === "/v1/me" ? [200, rachel] : [404, {}]));
    const onSignedIn = vi.fn();
    render(<SignIn onSignedIn={onSignedIn} identities={[
      { user: "alice", role: "editor", tenant: "acme", token: "alice-token" },
      { user: "rachel", role: "reviewer", tenant: "acme", token: "rachel-token" },
    ]} />);
    const buttons = screen.getAllByRole("button", { name: /acme/ });
    expect(buttons[0]).toHaveTextContent("rachel");  // reviewers listed before editors
    await userEvent.click(buttons[0]);
    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith("rachel-token", rachel));
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer rachel-token");
  });
});
