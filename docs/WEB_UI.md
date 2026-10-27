# Web UI

`web/` is a React + TypeScript + Vite single-page app styled with Tailwind CSS v4 covering the whole workflow: sign in with a bearer token, upload and ask documents, submit research jobs and approve or reject them as an independent reviewer, run the model release lifecycle (register → evaluate → baseline/canary → observe → promote or roll back), run the **Quality** scorecard on a labeled sample (precision, recall, F1 with 95% intervals, per-group results and plain-language gate failures), see each model's signed evidence and artifact re-hash history, and manage incidents. Uploads report how much PII was redacted before storage. Tabs follow your role: **Quality** and **Incidents** for admins and reviewers, **Releases** only for the `CAPSTONE_RELEASE_TENANT`.

**With Docker Compose** the `web` service builds the app and serves it with nginx on `http://127.0.0.1:${CAPSTONE_WEB_PORT:-8090}`, proxying `/v1` to the `api` service. The browser only talks to one origin, so the API needs no CORS. nginx sends a strict Content-Security-Policy (scripts and styles from this origin only).

**For development** (hot reload), with the API running locally:

```bash
cd web
npm install
VITE_API_TARGET=http://127.0.0.1:8000 npm run dev   # use 8002 if the Compose API is on CAPSTONE_PORT=8002
# open http://127.0.0.1:5173
npm test            # vitest + Testing Library
npm run build       # type-check and production bundle in web/dist
```

Sign in by pasting a token from `CAPSTONE_TOKENS_JSON`. Under `npm run dev` the sign-in page also lists those identities as one-click **Development only** buttons: `vite.config.ts` reads `../.env` only in dev-server mode, so production builds and tests never contain a token (set `VITE_DEV_SIGNIN=0` to hide them). The token is kept in `sessionStorage` (gone when the tab closes) and sent only as an `Authorization` header. Answers and reports are rendered as plain text, never HTML, because they quote untrusted documents. Static bearer tokens are a learning shortcut: a real deployment should sign in through an identity provider (OIDC) instead of pasting keys.

The UI uses read-only, tenant-scoped listing endpoints: `GET /v1/me`, `/v1/documents`, `/v1/research/jobs` (your jobs plus reports you may review), `/v1/models` (release tenant only) and `/v1/incidents` (admins and reviewers).
