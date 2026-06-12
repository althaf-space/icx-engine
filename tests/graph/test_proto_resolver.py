"""Tests for Phase 6: gRPC / Protocol Buffer resolver."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.proto_resolver import resolve_proto, _to_pascal


def _node(node_id, source_file):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": node_id}


class TestToPascal:
    def test_snake_to_pascal(self):
        assert _to_pascal("user_service") == "UserService"
        assert _to_pascal("greeter") == "Greeter"


class TestResolveProto:
    def test_no_proto_files_returns_empty(self):
        result = resolve_proto([], Path("."), {"nodes": []})
        assert result == []

    def test_proto_to_pb2_generated_edge(self, tmp_path):
        proto = tmp_path / "user_service.proto"
        proto.write_text('syntax = "proto3";\nservice UserService {}')
        pb2 = tmp_path / "user_service_pb2.py"
        pb2.write_text("# generated")
        pb2_grpc = tmp_path / "user_service_pb2_grpc.py"
        pb2_grpc.write_text("# generated grpc")

        proto_rel = str(proto.relative_to(tmp_path).as_posix())
        pb2_rel = str(pb2.relative_to(tmp_path).as_posix())
        pb2_grpc_rel = str(pb2_grpc.relative_to(tmp_path).as_posix())
        nodes = [
            _node("proto_n", proto_rel),
            _node("pb2_n", pb2_rel),
            _node("pb2_grpc_n", pb2_grpc_rel),
        ]
        edges = resolve_proto([proto, pb2, pb2_grpc], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "proto_generated" in types
        gen = [e for e in edges if e["type"] == "proto_generated"]
        assert all(e["confidence"] == 0.90 for e in gen)

    def test_python_servicer_creates_proto_implements(self, tmp_path):
        proto = tmp_path / "greeter.proto"
        proto.write_text('syntax = "proto3";\nservice GreeterService {}')
        impl = tmp_path / "server.py"
        impl.write_text("class MyGreeter(GreeterServiceServicer):\n    pass")

        proto_rel = str(proto.relative_to(tmp_path).as_posix())
        impl_rel = str(impl.relative_to(tmp_path).as_posix())
        nodes = [_node("proto_n", proto_rel), _node("impl_n", impl_rel)]
        edges = resolve_proto([proto, impl], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "proto_implements" in types
        pi = [e for e in edges if e["type"] == "proto_implements"][0]
        assert pi["confidence"] == 0.80

    def test_java_extends_grpc_impl_base(self, tmp_path):
        proto = tmp_path / "user.proto"
        proto.write_text('syntax = "proto3";\nservice UserService {}')
        java = tmp_path / "UserServiceImpl.java"
        java.write_text("class UserServiceImpl extends UserServiceGrpc.UserServiceImplBase {}")

        proto_rel = str(proto.relative_to(tmp_path).as_posix())
        java_rel = str(java.relative_to(tmp_path).as_posix())
        nodes = [_node("proto_n", proto_rel), _node("java_n", java_rel)]
        edges = resolve_proto([proto, java], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "proto_implements" in types

    def test_go_register_server_implements(self, tmp_path):
        proto = tmp_path / "hello.proto"
        proto.write_text('syntax = "proto3";\nservice HelloService {}')
        go = tmp_path / "main.go"
        go.write_text("func main() { pb.RegisterHelloServiceServer(s, &server{}) }")

        proto_rel = str(proto.relative_to(tmp_path).as_posix())
        go_rel = str(go.relative_to(tmp_path).as_posix())
        nodes = [_node("proto_n", proto_rel), _node("go_n", go_rel)]
        edges = resolve_proto([proto, go], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "proto_implements" in types

    def test_grpc_stub_creates_grpc_client(self, tmp_path):
        proto = tmp_path / "user_service.proto"
        proto.write_text('syntax = "proto3";\nservice UserService {}')
        client = tmp_path / "client.py"
        client.write_text("stub = user_service_pb2_grpc.UserServiceStub(channel)")

        proto_rel = str(proto.relative_to(tmp_path).as_posix())
        client_rel = str(client.relative_to(tmp_path).as_posix())
        nodes = [_node("proto_n", proto_rel), _node("client_n", client_rel)]
        edges = resolve_proto([proto, client], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "grpc_client" in types
        gc = [e for e in edges if e["type"] == "grpc_client"][0]
        assert gc["confidence"] == 0.75

    def test_proto_import_creates_proto_import_edge(self, tmp_path):
        proto_a = tmp_path / "common.proto"
        proto_a.write_text('syntax = "proto3";')
        proto_b = tmp_path / "service.proto"
        proto_b.write_text('syntax = "proto3";\nimport "common.proto";')

        a_rel = str(proto_a.relative_to(tmp_path).as_posix())
        b_rel = str(proto_b.relative_to(tmp_path).as_posix())
        nodes = [_node("a_n", a_rel), _node("b_n", b_rel)]
        edges = resolve_proto([proto_a, proto_b], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "proto_import" in types
        pi = [e for e in edges if e["type"] == "proto_import"][0]
        assert pi["confidence"] == 0.95


class TestProtoEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        proto_a = tmp_path / "common.proto"
        proto_a.write_text('syntax = "proto3";')
        proto_b = tmp_path / "service.proto"
        proto_b.write_text('syntax = "proto3";\nimport "common.proto";')
        a_rel = str(proto_a.relative_to(tmp_path).as_posix())
        b_rel = str(proto_b.relative_to(tmp_path).as_posix())
        nodes = [_node("a_n", a_rel), _node("b_n", b_rel)]
        edges = resolve_proto([proto_a, proto_b], tmp_path, {"nodes": nodes})
        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
