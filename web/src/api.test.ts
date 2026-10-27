import { describe, expect, it, vi } from "vitest";
import { ApiError, createClient } from "./api";

function fakeFetch(status: number, body: unknown) {
  return vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
    new Response(body === undefined ? "" : JSON.stringify(body), { status }));
}

describe("api client", () => {
  it("sends the bearer token and JSON body", async () => {
    const f = fakeFetch(200, { answer: "a", citations: [], mode: "offline-extractive" });
    await createClient("secret-token", f).ask("question?", "engineering");
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/v1/rag/query");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer secret-token");
    expect(JSON.parse(init?.body as string)).toEqual({ question: "question?", group: "engineering" });
  });

  it("omits an empty group", async () => {
    const f = fakeFetch(200, { answer: "a", citations: [], mode: "x" });
    await createClient("t", f).ask("q");
    expect(JSON.parse(f.mock.calls[0][1]?.body as string)).toEqual({ question: "q" });
  });

  it("attaches a valid idempotency key to new jobs", async () => {
    const f = fakeFetch(202, { job_id: "j1", status: "queued" });
    await createClient("t", f).createJob("q");
    const key = (f.mock.calls[0][1]?.headers as Record<string, string>)["Idempotency-Key"];
    expect(key).toMatch(/^[A-Za-z0-9_-]{8,80}$/);
  });

  it("surfaces the API's detail message", async () => {
    const f = fakeFetch(403, { detail: "Release operations not permitted for this tenant" });
    await expect(createClient("t", f).models()).rejects.toEqual(new ApiError(403, "Release operations not permitted for this tenant"));
  });

  it("encodes path parameters", async () => {
    const f = fakeFetch(200, { status: "contained" });
    await createClient("t", f).contain("../admin");
    expect(f.mock.calls[0][0]).toBe("/v1/incidents/..%2Fadmin/contain");
  });
});
