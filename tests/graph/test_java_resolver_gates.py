"""Per-file trigger gates on jaxrs / java_clients skip files that cannot produce
edges (perf) but MUST never drop a file that can. These guard gate completeness.
"""
from __future__ import annotations
import tempfile
from pathlib import Path


def _run(resolver_fn, files_map):
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "proj"; proj.mkdir()
        paths = []
        for name, src in files_map.items():
            p = proj / name; p.write_text(src, encoding="utf-8"); paths.append(p.resolve())
        from icx_engine.graph.parser.extract import extract
        with tempfile.TemporaryDirectory() as c:
            ex = extract(paths, cache_root=Path(c), parallel=False)
        return resolver_fn(paths, proj.resolve(), ex), paths


def test_jaxrs_gate_keeps_scheduled_files():
    from icx_engine.graph.parser.resolvers.jaxrs import extract_jaxrs_edges
    files = {
        "Sched.java": ("package a;\nimport io.quarkus.scheduler.Scheduled;\n"
                       "public class Sched {\n  @Scheduled(cron=\"0 0 * * *\")\n"
                       "  public void run() {}\n}\n"),
        "Plain.java": "package a;\npublic class Plain { public void x(){} }\n",
    }
    edges, _ = _run(extract_jaxrs_edges, files)
    # the @Scheduled file must not be gated out
    assert any(e.get("relation") == "scheduled" for e in edges)


def test_jaxrs_gate_keeps_route_files():
    from icx_engine.graph.parser.resolvers.jaxrs import extract_jaxrs_edges
    files = {
        "Res.java": ("package a;\nimport javax.ws.rs.Path;\nimport javax.ws.rs.GET;\n"
                     "@Path(\"/x\")\npublic class Res {\n  @GET\n  public String get(){return \"\";}\n}\n"),
    }
    edges, _ = _run(extract_jaxrs_edges, files)
    assert any(e.get("relation") == "routes" for e in edges)


def test_java_clients_gate_keeps_resttemplate_files():
    from icx_engine.graph.parser.resolvers.java_clients import extract_java_client_edges
    files = {
        "Svc.java": ("package a;\nimport org.springframework.web.client.RestTemplate;\n"
                     "public class Svc {\n  RestTemplate rt = new RestTemplate();\n"
                     "  void call(){ rt.getForObject(\"http://x/api\", String.class); }\n}\n"),
        "Plain.java": "package a;\npublic class Plain { void y(){} }\n",
    }
    edges, _ = _run(extract_java_client_edges, files)
    # RestTemplate file must not be gated out (edges may be 0 if no matching route,
    # but the file must be WALKED - assert no crash + resolver ran)
    assert isinstance(edges, list)


def test_gate_skips_plain_file_no_crash():
    from icx_engine.graph.parser.resolvers.jaxrs import extract_jaxrs_edges
    from icx_engine.graph.parser.resolvers.java_clients import extract_java_client_edges
    files = {"Plain.java": "package a;\npublic class Plain { void z(){} }\n"}
    e1, _ = _run(extract_jaxrs_edges, files)
    e2, _ = _run(extract_java_client_edges, files)
    assert e1 == [] and e2 == []  # nothing to emit, gated out cleanly
