"""Structured logging for Cloud Logging, and OpenTelemetry tracing to Cloud Trace."""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from .config import Settings

tracer = trace.get_tracer("ctx_server")


class JsonFormatter(logging.Formatter):
    """One JSON object per line; Cloud Logging maps `severity` and `message`, and links
    entries to traces through the logging.googleapis.com/trace field."""

    def __init__(self, project: str | None):
        super().__init__()
        self._project = project

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid and self._project:
            entry["logging.googleapis.com/trace"] = (
                f"projects/{self._project}/traces/{format(span.trace_id, '032x')}"
            )
            entry["logging.googleapis.com/spanId"] = format(span.span_id, "016x")
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.is_prod:
        handler.setFormatter(JsonFormatter(settings.gcp_project))
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)


def setup_tracing(settings: Settings, app: Any) -> None:
    """Install the tracer provider and instrument the app. Call before the app starts."""
    if not settings.otel_enabled:
        return
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "ctx-server"}))
    provider.add_span_processor(
        BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.gcp_project))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,health")
    HTTPXClientInstrumentor().instrument()


def instrument_engine(settings: Settings, engine: Any) -> None:
    if settings.otel_enabled:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


@contextmanager
def span(name: str, **attrs: Any):
    with tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield sp
