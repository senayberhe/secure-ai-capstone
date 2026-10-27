"""Single-process worker; multiple replicas use DB claiming/fencing. No raw-data logs."""
import logging
import os
import time
from .config import Settings
from .db import database
from .jobs import claim_job, finish_job, reconcile_reviews
from .queueing import RedisNotifier
from .telemetry import JOBS

log = logging.getLogger("capstone.worker")


def process_once(session_factory, engine="offline", backend="offline", writer=None, graph=None):
    with session_factory() as session:
        claim = claim_job(session)
    if not claim:
        return False
    if backend == "openai" and writer is None:
        from .db import ModelVersion
        from .llm import LLMWriter
        with session_factory() as session:
            version = session.get(ModelVersion, claim["model_version"]) if claim.get("model_version") else None
        model_id = version.provider_model if version and version.provider_model else os.environ["OPENAI_MODEL"]
        writer = LLMWriter(model_id)
    with session_factory() as session:
        success = finish_job(session, claim, engine=engine, writer=writer, graph=graph)
    JOBS.labels(status="completed" if success else "retry").inc()
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    _, sessions = database(settings.database_url)
    notifier = RedisNotifier(settings.redis_url) if settings.redis_url else None
    engine = os.getenv("CAPSTONE_GRAPH", "langgraph")
    if settings.mode not in ("openai", "offline"):
        raise RuntimeError("MODEL_BACKEND must be offline or openai")
    durable = None
    if engine == "langgraph":
        # Durable checkpoints: an interrupted research thread survives worker and API restarts.
        from .agent import build_durable_graph
        from .checkpoints import checkpoint_url, open_checkpointer
        saver, _close = open_checkpointer(checkpoint_url(settings.database_url))
        durable = build_durable_graph(sessions, saver)
    while True:
        try:
            if durable is not None:
                with sessions() as session:
                    reconcile_reviews(session, durable)
            if not process_once(sessions, engine=engine, backend=settings.mode, graph=durable):
                if notifier:
                    notifier.wait(2)
                else:
                    time.sleep(2)
        except Exception:
            log.error("Worker iteration failed (no raw exception logged)")
            time.sleep(2)


if __name__ == "__main__":
    main()
