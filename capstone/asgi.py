from .config import Settings
from .api import create_app

# Schema initialized by separate init_db command in Compose.
app = create_app(Settings.from_env())

# Explicitly opt into OTel instrumentation; no text/credential captures.
import os
if os.getenv("OTEL_ENABLED") == "1":
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
