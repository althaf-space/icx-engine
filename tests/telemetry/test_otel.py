from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from icx_engine.models.config import AppConfig, LangfuseConfig
from icx_engine.telemetry import otel


@pytest.fixture(autouse=True)
def _reset_provider_init():
    otel._provider_initialized = False
    yield
    otel._provider_initialized = False


# -- LocalJsonlSpanExporter - the always-on local record -----------------------------------

def _memory_tracer():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_local_exporter_writes_one_jsonl_line_per_span(tmp_path):
    tracer, exporter = _memory_tracer()
    span = tracer.start_span("git_push", start_time=1_000_000_000)
    span.end(end_time=1_000_500_000)
    finished = exporter.get_finished_spans()

    local_exporter = otel.LocalJsonlSpanExporter(root=tmp_path)
    result = local_exporter.export(finished)

    assert result == SpanExportResult.SUCCESS
    files = list(tmp_path.rglob("traces.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["name"] == "git_push"


def test_local_exporter_appends_across_calls(tmp_path):
    tracer, exporter = _memory_tracer()
    for tool_name in ("a", "b"):
        span = tracer.start_span(tool_name, start_time=1_000_000_000)
        span.end(end_time=1_000_100_000)
    finished = exporter.get_finished_spans()

    local_exporter = otel.LocalJsonlSpanExporter(root=tmp_path)
    local_exporter.export(finished[:1])
    local_exporter.export(finished[1:])

    files = list(tmp_path.rglob("traces.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_local_exporter_never_raises_on_write_failure(monkeypatch, tmp_path):
    local_exporter = otel.LocalJsonlSpanExporter(root=tmp_path)
    monkeypatch.setattr(otel.Path, "mkdir", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    result = local_exporter.export([])  # must not raise

    assert result == SpanExportResult.FAILURE


# -- _langfuse_processor - config-gated, independent of the local file guarantee -----------

def test_langfuse_processor_none_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "icx_engine.config_manager.ConfigManager.load",
        staticmethod(lambda: AppConfig(langfuse=LangfuseConfig(enabled=False))),
    )
    assert otel._langfuse_processor() is None


def test_langfuse_processor_none_when_enabled_but_keys_missing(monkeypatch):
    monkeypatch.setattr(
        "icx_engine.config_manager.ConfigManager.load",
        staticmethod(lambda: AppConfig(langfuse=LangfuseConfig(enabled=True, public_key="pk", secret_key=None))),
    )
    assert otel._langfuse_processor() is None


def test_langfuse_processor_attached_when_enabled_with_keys(monkeypatch):
    monkeypatch.setattr(
        "icx_engine.config_manager.ConfigManager.load",
        staticmethod(lambda: AppConfig(langfuse=LangfuseConfig(
            enabled=True, host="https://cloud.langfuse.com", public_key="pk-1", secret_key="sk-1",
        ))),
    )
    processor = otel._langfuse_processor()
    assert isinstance(processor, BatchSpanProcessor)


def test_langfuse_processor_none_on_config_load_failure(monkeypatch):
    def _boom():
        raise RuntimeError("no config")
    monkeypatch.setattr("icx_engine.config_manager.ConfigManager.load", staticmethod(_boom))
    assert otel._langfuse_processor() is None


# -- _generic_otlp_processor - separate, standard-env-var-driven, stackable with Langfuse --

def test_generic_otlp_processor_none_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert otel._generic_otlp_processor() is None


def test_generic_otlp_processor_attached_when_endpoint_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    processor = otel._generic_otlp_processor()
    assert isinstance(processor, BatchSpanProcessor)


# -- _ensure_provider - local processor always attached, network processors additive -------

def test_ensure_provider_always_installs_a_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(otel, "_langfuse_processor", lambda: None)
    # Redirect the local exporter away from the real ~/.icx/otel so this test writes nothing
    # outside tmp_path.
    _OriginalExporter = otel.LocalJsonlSpanExporter
    monkeypatch.setattr(otel, "LocalJsonlSpanExporter", lambda: _OriginalExporter(root=tmp_path))
    calls = []
    monkeypatch.setattr(otel.trace, "set_tracer_provider", lambda p: calls.append(p))

    otel._ensure_provider()

    assert len(calls) == 1


def test_ensure_provider_runs_once_per_process(tmp_path, monkeypatch):
    monkeypatch.setattr(otel, "_langfuse_processor", lambda: None)
    monkeypatch.setattr(otel, "_generic_otlp_processor", lambda: None)
    _OriginalExporter = otel.LocalJsonlSpanExporter
    monkeypatch.setattr(otel, "LocalJsonlSpanExporter", lambda: _OriginalExporter(root=tmp_path))
    calls = []
    monkeypatch.setattr(otel.trace, "set_tracer_provider", lambda p: calls.append(p))

    otel._ensure_provider()
    otel._ensure_provider()

    assert len(calls) == 1


def test_ensure_provider_attaches_local_processor_plus_configured_network_processors(monkeypatch):
    monkeypatch.setattr(otel, "_langfuse_processor", lambda: "langfuse-marker")
    monkeypatch.setattr(otel, "_generic_otlp_processor", lambda: "generic-marker")
    added = []

    class _FakeProvider:
        def __init__(self, resource=None):
            pass

        def add_span_processor(self, processor):
            added.append(processor)

    monkeypatch.setattr("opentelemetry.sdk.trace.TracerProvider", _FakeProvider)
    monkeypatch.setattr(otel.trace, "set_tracer_provider", lambda p: None)

    otel._ensure_provider()

    assert isinstance(added[0], SimpleSpanProcessor)
    assert "langfuse-marker" in added
    assert "generic-marker" in added


# -- record_tool_call - span content, unaffected by which processors are attached ----------

def test_record_tool_call_sets_attributes_and_ok_status(monkeypatch):
    tracer, exporter = _memory_tracer()
    monkeypatch.setattr(otel, "_ensure_provider", lambda: None)
    monkeypatch.setattr(otel.trace, "get_tracer", lambda name: tracer)

    otel.record_tool_call(
        "git_repo_status", '{"a": 1}', '{"ok": true}',
        1_000_000_000, 1_000_500_000, ok=True, error_type=None,
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "git_repo_status"
    assert span.attributes["icx.tool.name"] == "git_repo_status"
    assert span.attributes["icx.tool.ok"] is True
    assert span.attributes["icx.tool.input_bytes"] > 0
    assert span.attributes["icx.tool.output_bytes"] > 0
    assert span.attributes["icx.tool.input_tokens_est"] > 0
    assert span.attributes["icx.tool.output_tokens_est"] > 0
    assert "icx.tool.error_type" not in span.attributes
    assert span.status.status_code.name == "OK"


def test_record_tool_call_sets_error_status_and_attribute(monkeypatch):
    tracer, exporter = _memory_tracer()
    monkeypatch.setattr(otel, "_ensure_provider", lambda: None)
    monkeypatch.setattr(otel.trace, "get_tracer", lambda name: tracer)

    otel.record_tool_call(
        "git_push", "{}", None,
        1_000_000_000, 1_000_100_000, ok=False, error_type="GitWorkflowError",
    )

    spans = exporter.get_finished_spans()
    span = spans[0]
    assert span.attributes["icx.tool.ok"] is False
    assert span.attributes["icx.tool.error_type"] == "GitWorkflowError"
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["icx.tool.output_bytes"] == 0


def test_record_tool_call_never_raises_when_tracing_itself_fails(monkeypatch):
    def _boom(name):
        raise RuntimeError("boom")
    monkeypatch.setattr(otel, "_ensure_provider", lambda: None)
    monkeypatch.setattr(otel.trace, "get_tracer", _boom)

    otel.record_tool_call("a", "{}", "{}", 1, 2, ok=True, error_type=None)  # must not raise
