# Threat model and control evidence

## Scope and assets

Assets: customer documents, source metadata, research reports, authenticated identities, private model inputs, API credentials, evaluation/approval records, release decisions, operational audit events.

Adversary may submit authorized lower-trust documents, input text, unknown citation strings, repeated API calls, guessed document/job IDs, and prompt-injection content. Adversary **does not** control database schema, server authentication config, CI release identities, or Python source code in this demonstration. Those are separate threats for a deployed environment.

## STRIDE-inspired matrix

| Threat | Attack surface | Mitigation / test | Residual risk |
|---|---|---|---|
| Spoofing | Bearer auth | Server-held tokens; compare before authorization | Static keys; no expiry or central revocation |
| Tampering | Retrieved passages | Treat as evidence; fixed tool graph | Optional live LLM can follow malicious text |
| Repudiation | Approval decisions | Durable approval and event rows | Need immutable audit retention and traceable identities |
| Information disclosure | Cross-tenant retrieval | SQL filter before embedding / ranking | DB administrators and backups need separate controls |
| Information disclosure | Error messages and metrics | Generic validation and safe metric label cardinality | Regex redaction incomplete; traces must be configured safely |
| Denial of service | Flood of API requests | Redis Lua rate limit; request bounds | Token/cost budgets and body limits at ingress still needed |
| Elevation of privilege | Research tool / approval | No arbitrary tools; distinct reviewer and ownership checks | Model-code compromise, runtime misconfiguration, privileged DB accounts |
| Stale-job race | Worker leases | Attempt fencing and status predicate | External provider calls not exactly-once |
| Compromised model | Registry / weights | Version keys and expected hashes | Actual provenance attestation and artifact verification not implemented |
| Misleading release | Admin-supplied scores | Independent review, evidence IDs tied to version | Telemetry evidence isn't signed/derived from immutable test runs |

## Input trust boundaries

- Client never supplies the trusted tenant/user/role.
- The API never exposes a shell, arbitrary SQL, URL fetch, or filesystem writing tool to a model.
- Retrieved passages may contain strings that resemble instructions. The offline agent extracts them as quoted content but doesn't execute them.
- A malicious line in a retrieved document can still appear in the extractive report, so downstream rendering and people must see it as *source text*, not authoritative instructions.
- Optional LLM generation is gated to previously authorized passages but remains susceptible to linguistic manipulation; run separate live red-team evaluation.

## High-priority production work

1. Replace static tokens with an external identity provider, short-lived credentials, role separation, and revocation.
2. Add durable encryption/key management, tenant-specific retrieval indices as appropriate, data retention and deletion.
3. Use a restricted network egress policy; an agent doesn't need arbitrary outbound connections.
4. Add immutable approval and model artifact evidence; attest evaluation datasets and run outputs.
5. Perform full SSRF/XSS/DoS testing on any new connectors, document parsers, and browser or code tools.
6. Add cost budgets, concurrency caps, queued-job TTL, and circuit breakers around external model calls.
7. Perform response-grounding evaluations, not just citation-ID checking.
