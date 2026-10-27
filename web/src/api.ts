// Typed client for the capstone API. All calls are same-origin (proxied in dev and in
// Docker), and the bearer token never leaves this module except in the Authorization header.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export type Me = { user: string; tenant: string; role: string; groups: string[]; release_operator: boolean };
export type DocumentRow = { document_id: string; title: string; group: string; owner: string; created_at: string };
export type Citation = { citation_id: string; document_id: string; title: string };
export type Answer = { answer: string; citations: Citation[]; mode: string };
export type JobStatus = "queued" | "running" | "pending_review" | "approved" | "rejected" | "failed";
export type JobRow = {
  job_id: string; owner: string; question: string; status: JobStatus;
  model_version: string | null; created_at: string; can_review: boolean;
};
export type JobDetail = { job_id: string; status: JobStatus; report: string | null; citations: string[]; model_version: string | null };
export type ModelRow = {
  version: string; status: string; traffic_pct: number; provider_model: string | null;
  registered_by: string; reviewed_by: string | null;
  observed_requests: number; observed_errors: number; observed_critical: number;
};
export type IncidentRow = { incident_id: string; category: string; summary: string; status: string; opened_by: string };
export type Interval = [number, number] | null;
export type GroupMetrics = {
  n: number; positives: number; tp: number; fp: number; tn: number; fn: number;
  accuracy: number; accuracy_ci95: Interval; precision: number | null; precision_ci95: Interval;
  recall: number | null; recall_ci95: Interval; f1: number | null; positive_rate: number;
};
export type ScoreRow = { true: 0 | 1; pred: 0 | 1; group: string; unsupported?: boolean; leak?: boolean };
export type Scorecard = {
  n: number; accuracy: number; accuracy_ci95: Interval; precision: number | null; precision_ci95: Interval;
  recall: number | null; recall_ci95: Interval; f1: number | null; unsupported_rate: number; leaks: number;
  selection_rate_gap: number; recall_gap: number | null; groups: Record<string, GroupMetrics>;
  decision: "HOLD" | "READY_FOR_HUMAN_REVIEW"; failed_gates: string[];
};
export type EvidenceHistory = {
  evidence: { evidence_id: string; decision: string; attester: string; artifact_sha256: string;
              dataset_sha256: string; attested_at: string; key_id: string }[];
  artifact_verifications: { sha256: string; matched: boolean; verified_by: string; verified_at: string }[];
};
export type EvidenceEnvelope = { payload: string; signature: string; key_id: string };
export type EvidenceReceipt = { evidence_id: string; version: string; decision: string; attester: string; artifact_sha256: string };

type Fetch = typeof fetch;

async function parse(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function createClient(token: string, fetchImpl: Fetch = (...args) => fetch(...args)) {
  async function request<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
    const res = await fetchImpl(path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await parse(res);
    if (!res.ok) {
      const detail = data && typeof data === "object" && "detail" in data && typeof data.detail === "string"
        ? data.detail : `Request failed (${res.status})`;
      throw new ApiError(res.status, detail);
    }
    return data as T;
  }
  const get = <T,>(path: string) => request<T>("GET", path);
  const post = <T,>(path: string, body?: unknown, headers?: Record<string, string>) => request<T>("POST", path, body, headers);

  return {
    me: () => get<Me>("/v1/me"),
    documents: () => get<{ documents: DocumentRow[] }>("/v1/documents").then((r) => r.documents),
    upload: (title: string, group: string, text: string) =>
      post<{ document_id: string; chunks: number; redactions: Record<string, number> }>("/v1/documents", { title, group, text }),
    ask: (question: string, group?: string) =>
      post<Answer>("/v1/rag/query", { question, ...(group ? { group } : {}) }),

    jobs: () => get<{ jobs: JobRow[] }>("/v1/research/jobs").then((r) => r.jobs),
    job: (id: string) => get<JobDetail>(`/v1/research/jobs/${encodeURIComponent(id)}`),
    createJob: (question: string, group?: string) =>
      post<{ job_id: string; status: JobStatus }>("/v1/research/jobs", { question, ...(group ? { group } : {}) },
        // A fresh key per submission; the API dedupes retries of the same key.
        { "Idempotency-Key": crypto.randomUUID() }),
    review: (id: string, approved: boolean) =>
      post<{ job_id: string; status: JobStatus }>(`/v1/research/jobs/${encodeURIComponent(id)}/review`, { approved }),

    assignment: () => get<{ version: string | null }>("/v1/releases/assignment"),
    models: () => get<{ models: ModelRow[] }>("/v1/models").then((r) => r.models),
    register: (version: string, sha256: string, artifact_path: string, provider_model?: string) =>
      post<{ version: string; status: string }>("/v1/models/register",
        { version, sha256, artifact_path, ...(provider_model ? { provider_model } : {}) }),
    // Evidence is produced and signed by CI; the UI only relays it for server-side verification.
    submitEvidence: (envelope: EvidenceEnvelope) => post<EvidenceReceipt>("/v1/models/evidence", envelope),
    evidenceHistory: (version: string) => get<EvidenceHistory>(`/v1/models/${encodeURIComponent(version)}/evidence`),
    scorecard: (records: ScoreRow[]) => post<Scorecard>("/v1/scorecards", { records }),
    bootstrap: (version: string, evaluation_id: string) =>
      post<{ version: string; status: string }>("/v1/releases/bootstrap", { version, evaluation_id }),
    canary: (version: string, evaluation_id: string, percent: number) =>
      post<{ version: string; traffic_pct: number; status: string }>("/v1/releases/canary", { version, evaluation_id, percent }),
    observe: (version: string, requests: number, errors: number, critical_events: number) =>
      post<{ decision: string; error_rate: number }>("/v1/releases/observe", { version, requests, errors, critical_events }),
    promote: (version: string) => post<{ status: string }>("/v1/releases/promote", { version }),
    rollback: (version: string) => post<{ status: string }>("/v1/releases/rollback", { version }),

    incidents: () => get<{ incidents: IncidentRow[] }>("/v1/incidents").then((r) => r.incidents),
    openIncident: (category: string, summary: string) =>
      post<{ incident_id: string; status: string }>("/v1/incidents", { category, summary }),
    contain: (id: string) => post<{ status: string }>(`/v1/incidents/${encodeURIComponent(id)}/contain`),
    resolve: (id: string) => post<{ status: string }>(`/v1/incidents/${encodeURIComponent(id)}/resolve`),
  };
}

export type Client = ReturnType<typeof createClient>;
