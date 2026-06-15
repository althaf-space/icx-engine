from pathlib import Path

from icx_engine.graph.parser.resolvers.swift_resolver import resolve_swift


def _node(node_id, name, source_file):
    return {"id": node_id, "name": name, "source_file": source_file}


class TestSwiftResolverNoOp:
    def test_no_swift_files_returns_empty(self, tmp_path):
        assert resolve_swift([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        swift_file = tmp_path / "main.swift"
        swift_file.write_text("print(\"hi\")\n", encoding="utf-8")
        assert resolve_swift([swift_file], tmp_path, {"nodes": []}) == []


class TestSwiftImport:
    def test_import_resolves_to_matching_directory(self, tmp_path):
        networking_dir = tmp_path / "Networking"
        networking_dir.mkdir()
        client = networking_dir / "Client.swift"
        client.write_text("public class Client {\n    func run() {}\n}\n", encoding="utf-8")

        app_dir = tmp_path / "App"
        app_dir.mkdir()
        main = app_dir / "Main.swift"
        main.write_text("import Networking\n\nclass Main {\n    func run() {}\n}\n", encoding="utf-8")

        nodes = [
            _node("main_n", "Main", "App/Main.swift"),
            _node("client_n", "Client", "Networking/Client.swift"),
        ]
        edges = resolve_swift([main, client], tmp_path, {"nodes": nodes})
        import_edges = [e for e in edges if e["relation"] == "swift_import"]
        assert ("main_n", "client_n") in [(e["source"], e["target"]) for e in import_edges]


class TestSwiftConforms:
    def test_type_conforms_to_protocol_in_other_file(self, tmp_path):
        proto = tmp_path / "Runnable.swift"
        proto.write_text("protocol Runnable {\n    func run()\n}\n", encoding="utf-8")
        impl = tmp_path / "Worker.swift"
        impl.write_text("class Worker: Runnable {\n    func run() {}\n}\n", encoding="utf-8")

        nodes = [
            _node("worker_n", "Worker", "Worker.swift"),
            _node("runnable_n", "Runnable", "Runnable.swift"),
        ]
        edges = resolve_swift([proto, impl], tmp_path, {"nodes": nodes})
        conforms_edges = [e for e in edges if e["relation"] == "swift_conforms"]
        assert ("worker_n", "runnable_n") in [(e["source"], e["target"]) for e in conforms_edges]


class TestSwiftCalls:
    def test_intra_directory_function_call(self, tmp_path):
        helper = tmp_path / "Helper.swift"
        helper.write_text("func helper() -> Int {\n    return 42\n}\n", encoding="utf-8")
        main = tmp_path / "Main.swift"
        main.write_text("func main() {\n    _ = helper()\n}\n", encoding="utf-8")

        nodes = [
            _node("main_n", "main", "Main.swift"),
            _node("helper_n", "helper", "Helper.swift"),
        ]
        edges = resolve_swift([main, helper], tmp_path, {"nodes": nodes})
        call_edges = [e for e in edges if e["relation"] == "swift_calls"]
        assert ("main_n", "helper_n") in [(e["source"], e["target"]) for e in call_edges]
