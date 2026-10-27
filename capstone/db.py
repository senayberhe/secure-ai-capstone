"""SQLAlchemy schema and database factories; SQLite for tests, PostgreSQL for Compose."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import String, Integer, Text, Float, DateTime, ForeignKey, UniqueConstraint, Index, LargeBinary, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(64))
    access_group: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_doc_scope", "tenant", "access_group"),)
    # Declared so the unit of work always inserts a document before its chunks (FK order).
    chunks: Mapped[list["Chunk"]] = relationship(lazy="raise")


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    access_group: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)


class ChunkEmbedding(Base):
    """Cached document vector per chunk and embedding model (float32 bytes)."""
    __tablename__ = "chunk_embeddings"
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class Job(Base):
    __tablename__ = "research_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    groups_json: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    requested_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idem_key: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("tenant", "owner", "idem_key", name="uq_job_idem"),)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("research_jobs.id"), unique=True)
    tenant: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(24), default="pending")


class ModelVersion(Base):
    __tablename__ = "model_versions"
    version: Mapped[str] = mapped_column(String(80), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    provider_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    registered_by: Mapped[str] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="registered")
    traffic_pct: Mapped[int] = mapped_column(Integer, default=0)
    baseline: Mapped[str | None] = mapped_column(String(80), nullable=True)
    observed_requests: Mapped[int] = mapped_column(Integer, default=0)
    observed_errors: Mapped[int] = mapped_column(Integer, default=0)
    observed_critical: Mapped[int] = mapped_column(Integer, default=0)


class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    model_version: Mapped[str] = mapped_column(ForeignKey("model_versions.version"), index=True)
    samples: Mapped[int] = mapped_column(Integer)
    accuracy: Mapped[float] = mapped_column(Float)
    unsupported_rate: Mapped[float] = mapped_column(Float)
    leakage_count: Mapped[int] = mapped_column(Integer)
    p95_ms: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(12))


class ModelArtifact(Base):
    """Where the approved artifact bytes live, relative to CAPSTONE_ARTIFACT_ROOT."""
    __tablename__ = "model_artifacts"
    version: Mapped[str] = mapped_column(ForeignKey("model_versions.version"), primary_key=True)
    path: Mapped[str] = mapped_column(String(255))


class ReleaseEvidence(Base):
    """Immutable, signature-verified CI evaluation report bound to one version and artifact digest."""
    __tablename__ = "release_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    model_version: Mapped[str] = mapped_column(ForeignKey("model_versions.version"), index=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    key_id: Mapped[str] = mapped_column(String(32))
    attester: Mapped[str] = mapped_column(String(100))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    dataset_sha256: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(12))
    envelope: Mapped[str] = mapped_column(Text)
    attested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_by: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ArtifactVerification(Base):
    """Server-side re-hash of artifact bytes by an identity independent of the registrar."""
    __tablename__ = "artifact_verifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    model_version: Mapped[str] = mapped_column(ForeignKey("model_versions.version"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    matched: Mapped[bool] = mapped_column()
    verified_by: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    tenant: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="open")
    category: Mapped[str] = mapped_column(String(60))
    summary: Mapped[str] = mapped_column(String(400))
    opened_by: Mapped[str] = mapped_column(String(64))


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    tenant: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(60))
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def database(url: str):
    options = {"future": True, "pool_pre_ping": True}
    if url == "sqlite://":
        options.update({"connect_args": {"check_same_thread": False}, "poolclass": StaticPool})
    elif url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **options)
    if url.startswith("sqlite"):
        # SQLite ignores foreign keys unless asked; enforce them so tests match PostgreSQL.
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


# Release evidence and verifications are append-only, and a registered digest never changes.
# Enforced in the database so that no code path, script or console session can rewrite them.
SQLITE_IMMUTABILITY = [
    *[f"""CREATE TRIGGER IF NOT EXISTS {t}_no_{op} BEFORE {op.upper()} ON {t}
          BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END""" for t in ("release_evidence", "artifact_verifications")
      for op in ("update", "delete")],
    """CREATE TRIGGER IF NOT EXISTS model_versions_digest_fixed BEFORE UPDATE OF artifact_sha256 ON model_versions
       WHEN NEW.artifact_sha256 IS NOT OLD.artifact_sha256 BEGIN SELECT RAISE(ABORT, 'registered digest is immutable'); END""",
]
POSTGRES_IMMUTABILITY = [
    """CREATE OR REPLACE FUNCTION capstone_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ' is append-only'; END $$ LANGUAGE plpgsql""",
    """CREATE OR REPLACE FUNCTION capstone_digest_fixed() RETURNS trigger AS $$
       BEGIN IF NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256 THEN
         RAISE EXCEPTION 'registered digest is immutable'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql""",
    *[stmt for t in ("release_evidence", "artifact_verifications") for stmt in (
        f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}",
        f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION capstone_append_only()")],
    "DROP TRIGGER IF EXISTS model_versions_digest_fixed ON model_versions",
    "CREATE TRIGGER model_versions_digest_fixed BEFORE UPDATE ON model_versions FOR EACH ROW EXECUTE FUNCTION capstone_digest_fixed()",
]


def init_schema(engine):
    Base.metadata.create_all(engine)  # Demo only: replace with Alembic migrations in production.
    statements = {"sqlite": SQLITE_IMMUTABILITY, "postgresql": POSTGRES_IMMUTABILITY}.get(engine.dialect.name, [])
    with engine.begin() as con:
        for statement in statements:
            con.exec_driver_sql(statement)
