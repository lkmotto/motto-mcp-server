"""OpenTelemetry + Langfuse observability scaffolding.

Call init_observability("<agent-name>") once at startup. Every LLM call wrapped
in ``@traced`` (or manually with ``tracer.start_as_current_span``) is then visible
in Langfuse with cost, latency, prompt, and completion.
"""
from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def _langfuse_credentials_configured() -> bool:
    """Return True if both Langfuse key env vars are set."""
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def _build_auth_header() -> dict[str, str]:
    """Build Basic auth header from Langfuse API keys, if both are set."""
    import base64

    pk = os.environ["LANGFUSE_PUBLIC_KEY"]
    sk = os.environ["LANGFUSE_SECRET_KEY"]
    colon_joined = pk + ":" + sk
    encoded = base64.b64encode(colon_joined.encode()).decode()
    return {"Authorization": "Basic " + encoded}


def init_observability(service_name: str) -> trace.Tracer:
    """Initialize OTel tracing pointed at Langfuse. Idempotent."""
    if trace.get_tracer_provider().__class__.__name__ == "TracerProvider":
        return trace.get_tracer(service_name)

    endpoint = os.getenv(
        "LANGFUSE_OTEL_ENDPOINT",
        "https://us.cloud.langfuse.com/api/public/otel/v1/traces",
    )

    headers: dict[str, str] = {}
    if _langfuse_credentials_configured():
        headers = _build_auth_header()

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
