"""Durable LangGraph checkpointers: PostgreSQL in Compose, SQLite file locally, memory in tests.

The checkpoint location defaults to DATABASE_URL, so research threads live next to the job
and approval records. Override with CAPSTONE_CHECKPOINT_URL.
"""
from __future__ import annotations
import os
import sqlite3
import time

SETUP_LOCK = 7_412_026  # arbitrary application-wide advisory lock id for checkpoint schema setup


def checkpoint_url(database_url: str) -> str:
    return os.getenv("CAPSTONE_CHECKPOINT_URL") or database_url


def open_checkpointer(url: str):
    """Return (checkpointer, close). Tables are created on first use."""
    if url.startswith("postgresql"):
        from psycopg import Connection
        from psycopg.rows import dict_row
        from langgraph.checkpoint.postgres import PostgresSaver
        # psycopg wants a libpq URL, not SQLAlchemy's "postgresql+psycopg://" form.
        conn = Connection.connect(url.replace("postgresql+psycopg://", "postgresql://", 1),
                                  autocommit=True, prepare_threshold=0, row_factory=dict_row)
        saver = PostgresSaver(conn)
        # API, worker and replicas start together; serialise table creation across processes.
        # Poll with try-lock instead of blocking: setup runs CREATE INDEX CONCURRENTLY, which waits
        # for every open transaction, including a session blocked in pg_advisory_lock (deadlock).
        while not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (SETUP_LOCK,)).fetchone()["ok"]:
            time.sleep(0.1)
        try:
            saver.setup()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (SETUP_LOCK,))
        return saver, conn.close
    if url.startswith("sqlite:///"):
        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = sqlite3.connect(url.removeprefix("sqlite:///"), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver, conn.close
    if url == "sqlite://":
        # Process memory only: fine for tests, NOT durable across restarts.
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver(), lambda: None
    raise ValueError("Unsupported checkpoint URL")
