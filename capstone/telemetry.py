"""Aggregate metrics without raw prompts, tenants or user-id labels."""
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

REQUESTS = Counter("capstone_http_requests_total", "Requests", ["route", "method", "status"])
DURATION = Histogram("capstone_http_duration_seconds", "Request duration", ["route", "method"])
JOBS = Counter("capstone_worker_jobs_total", "Worker completions", ["status"])


def metric_bytes():
    return generate_latest()
