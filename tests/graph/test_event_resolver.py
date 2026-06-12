"""Tests for Phase 8: Cross-service event detection."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.event_resolver import resolve_events


def _node(node_id, source_file):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": node_id}


class TestKafka:
    def test_python_producer_creates_kafka_publish(self, tmp_path):
        pub = tmp_path / "publisher.py"
        pub.write_text("producer.send('orders.created', value=data)")
        rel = str(pub.relative_to(tmp_path).as_posix())
        nodes = [_node("pub_n", rel)]
        edges = resolve_events([pub], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "kafka_publish" in types
        kp = [e for e in edges if e["type"] == "kafka_publish"][0]
        assert kp["confidence"] == 0.80

    def test_java_kafka_listener_creates_kafka_subscribe(self, tmp_path):
        sub = tmp_path / "Consumer.java"
        sub.write_text('@KafkaListener(topics = "orders.created")\npublic void consume(String msg) {}')
        rel = str(sub.relative_to(tmp_path).as_posix())
        nodes = [_node("sub_n", rel)]
        edges = resolve_events([sub], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "kafka_subscribe" in types

    def test_same_topic_publisher_and_subscriber_creates_event_channel(self, tmp_path):
        pub = tmp_path / "publisher.py"
        pub.write_text("producer.send('payments', value=data)")
        sub = tmp_path / "Consumer.java"
        sub.write_text('@KafkaListener(topics = "payments")\npublic void handle(String m) {}')

        pub_rel = str(pub.relative_to(tmp_path).as_posix())
        sub_rel = str(sub.relative_to(tmp_path).as_posix())
        nodes = [_node("pub_n", pub_rel), _node("sub_n", sub_rel)]
        edges = resolve_events([pub, sub], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "event_channel" in types
        ec = [e for e in edges if e["type"] == "event_channel"][0]
        assert ec["confidence"] == 0.75


class TestRabbitMQ:
    def test_basic_publish_creates_rabbitmq_publish(self, tmp_path):
        pub = tmp_path / "publisher.py"
        pub.write_text("channel.basic_publish(exchange='', routing_key='payments', body=msg)")
        rel = str(pub.relative_to(tmp_path).as_posix())
        nodes = [_node("pub_n", rel)]
        edges = resolve_events([pub], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "rabbitmq_publish" in types
        rp = [e for e in edges if e["type"] == "rabbitmq_publish"][0]
        assert rp["confidence"] == 0.75


class TestRedis:
    def test_redis_publish_creates_redis_publish(self, tmp_path):
        pub = tmp_path / "notifier.py"
        pub.write_text("redis.publish('notifications', json.dumps(event))")
        rel = str(pub.relative_to(tmp_path).as_posix())
        nodes = [_node("pub_n", rel)]
        edges = resolve_events([pub], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "redis_publish" in types
        rp = [e for e in edges if e["type"] == "redis_publish"][0]
        assert rp["confidence"] == 0.70


class TestOpenAPI:
    def test_openapi_yaml_to_app_py_creates_openapi_impl(self, tmp_path):
        spec = tmp_path / "openapi.yaml"
        spec.write_text("openapi: 3.0.0\ninfo:\n  title: API\n  version: 1.0.0")
        app = tmp_path / "app.py"
        app.write_text("from flask import Flask\napp = Flask(__name__)")

        spec_rel = str(spec.relative_to(tmp_path).as_posix())
        app_rel = str(app.relative_to(tmp_path).as_posix())
        nodes = [_node("spec_n", spec_rel), _node("app_n", app_rel)]
        edges = resolve_events([spec, app], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "openapi_impl" in types
        oi = [e for e in edges if e["type"] == "openapi_impl"][0]
        assert oi["confidence"] == 0.85


class TestEventEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        pub = tmp_path / "publisher.py"
        pub.write_text("producer.send('orders', value=data)")
        rel = str(pub.relative_to(tmp_path).as_posix())
        nodes = [_node("pub_n", rel)]
        edges = resolve_events([pub], tmp_path, {"nodes": nodes})
        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)


class TestEmpty:
    def test_no_broker_patterns_returns_empty(self, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("x = 1 + 1")
        rel = str(f.relative_to(tmp_path).as_posix())
        nodes = [_node("n", rel)]
        result = resolve_events([f], tmp_path, {"nodes": nodes})
        assert result == []

    def test_empty_files_returns_empty(self):
        result = resolve_events([], Path("."), {"nodes": []})
        assert result == []
