"""OpenTelemetry span emission for MCP tool calls - one real span per call, always persisted
locally under ~/.icx/otel/YYYY-MM-DD/traces.jsonl (IST-dated, 0o700, mirrors telemetry/logger.py's
JSONL log's durability and local-only-by-default posture). This is unconditional - unlike an
opt-in-via-env-var design, a fresh install with zero configuration still gets a complete OTel
trace on disk from the first tool call.

Export to Langfuse (or any other OTLP backend) is a SEPARATE, additive destination, gated by
explicit config (AppConfig.langfuse.enabled - see `icx langfuse`), not env-var presence: local
traces are the guarantee, Langfuse is opt-in on top of it. The generic OTEL_EXPORTER_OTLP_ENDPOINT
env var is also honored as a second, independent optional destination for any other OTLP backend -
it can be set alongside or instead of the Langfuse config, both attach as separate processors on
the same provider.

Parallel to, not a replacement for, ToolCallLogger - that JSONL log is unaffected by anything
here. record_tool_call() never raises past its own boundary, mirroring
ToolCallLogger.log_call()'s guarantee: a telemetry failure must never affect the tool call it
describes. Span attributes/local file records never contain tool-call content, only byte counts/
token estimates/ok/error_type - see developer.md's "Telemetry never logs secrets" note."""
from __future__ import annotations

import base64
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from icx_engine.telemetry.logger import estimate_tokens

_IST = timezone(timedelta(hours=5, minutes=30))
_provider_initialized = False


class LocalJsonlSpanExporter(SpanExporter):
    """Writes each finished span as one JSON line (ReadableSpan.to_json(indent=None)) to
    ~/.icx/otel/YYYY-MM-DD/traces.jsonl - the always-on local record. Never raises; a write
    failure here must never break span export for other processors on the same provider."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (Path.home() / ".icx" / "otel")

    def export(self, spans):
        try:
            now = datetime.now(_IST)
            day_dir = self._root / now.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
            with open(day_dir / "traces.jsonl", "a", encoding="utf-8") as f:
                for span in spans:
                    f.write(span.to_json(indent=None) + "\n")
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _langfuse_processor():
    """A BatchSpanProcessor exporting to Langfuse's OTel endpoint, or None when
    AppConfig.langfuse.enabled is False or credentials are incomplete - local trace file
    export is entirely unaffected either way."""
    try:
        from icx_engine.config_manager import ConfigManager
        cfg = ConfigManager.load().langfuse
    except Exception:
        return None
    if not (cfg.enabled and cfg.public_key and cfg.secret_key):
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    auth = base64.b64encode(f"{cfg.public_key}:{cfg.secret_key}".encode()).decode()
    endpoint = cfg.host.rstrip("/") + "/api/public/otel"
    exporter = OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": f"Basic {auth}"})
    return BatchSpanProcessor(exporter)


def _generic_otlp_processor():
    """A BatchSpanProcessor for any other OTLP backend, standard-env-var driven
    (OTEL_EXPORTER_OTLP_ENDPOINT/_TRACES_ENDPOINT + OTEL_EXPORTER_OTLP_HEADERS) - independent
    of, and stackable with, the Langfuse-specific config above."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if not endpoint:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    return BatchSpanProcessor(OTLPSpanExporter())


def _ensure_provider() -> None:
    """Installs a real TracerProvider exactly once per process - always, unconditionally, with
    the local file processor attached (the "complete OTel trace, always" guarantee) - plus
    the Langfuse and/or generic OTLP processors when configured."""
    global _provider_initialized
    if _provider_initialized:
        return
    _provider_initialized = True
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "icx-engine")})
    provider = TracerProvider(resource=resource)
    # SimpleSpanProcessor (synchronous, exports right after span.end()) for the local file -
    # same immediate-durability reasoning as ToolCallLogger's per-call write, so a crash right
    # after a call doesn't lose it. Network exporters below use BatchSpanProcessor instead,
    # standard practice for anything going over the wire.
    provider.add_span_processor(SimpleSpanProcessor(LocalJsonlSpanExporter()))
    for processor in (_langfuse_processor(), _generic_otlp_processor()):
        if processor is not None:
            provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)


def record_tool_call(
    name: str, input_text: str, output_text: str | None,
    start_ns: int, end_ns: int, ok: bool, error_type: str | None,
) -> None:
    """One span per MCP tool call, named after the tool - mirrors ToolCallLogger.log_call's
    signature so it drops into the same call site. Never raises."""
    try:
        _ensure_provider()
        tracer = trace.get_tracer("icx_engine.mcp")
        span = tracer.start_span(name, start_time=start_ns)
        span.set_attribute("icx.tool.name", name)
        span.set_attribute("icx.tool.input_bytes", len(input_text.encode("utf-8")))
        span.set_attribute("icx.tool.output_bytes", len(output_text.encode("utf-8")) if output_text is not None else 0)
        span.set_attribute("icx.tool.input_tokens_est", estimate_tokens(input_text))
        span.set_attribute("icx.tool.output_tokens_est", estimate_tokens(output_text or ""))
        span.set_attribute("icx.tool.ok", ok)
        if error_type:
            span.set_attribute("icx.tool.error_type", error_type)
            span.set_status(Status(StatusCode.ERROR, error_type))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end(end_time=end_ns)
    except Exception:
        pass
