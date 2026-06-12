"""
Cross-service event-driven edge resolver for ICX graph.

Detects: Kafka, RabbitMQ, Redis pub/sub, AWS SQS/SNS, NATS,
         OpenAPI/Swagger specs, AsyncAPI specs.

Edge types:
  kafka_publish / kafka_subscribe          (0.80)
  rabbitmq_publish / rabbitmq_subscribe    (0.75)
  redis_publish / redis_subscribe          (0.70)
  sqs_publish / sqs_subscribe              (0.75)
  sns_publish                              (0.75)
  nats_publish / nats_subscribe            (0.70)
  event_channel  (0.75): publisher file -> subscriber file through shared topic
  openapi_impl   (0.85): OpenAPI/Swagger spec -> entrypoint file
  asyncapi_impl  (0.85): AsyncAPI spec -> entrypoint file

Activation: always runs - checks all files for broker patterns.
"""
import re
from pathlib import Path
from collections import defaultdict

# -- Kafka --
_KAFKA_PY_SEND   = re.compile(r'(?:producer|kafka)\.send\s*\(\s*["\']([^"\']+)["\']', re.M)
_KAFKA_PY_SUB    = re.compile(r'consumer\.subscribe\s*\(\s*\[([^\]]+)\]', re.M)
_KAFKA_JAVA_LIST = re.compile(r'@KafkaListener\s*\(\s*topics\s*=\s*["\{]([^"}\)]+)["\}]', re.M)
_KAFKA_JAVA_SEND = re.compile(r'kafkaTemplate\.send\s*\(\s*"([^"]+)"', re.M)
_KAFKA_JS_PROD   = re.compile(r'producer\.send\s*\(\s*\{[^}]*topic\s*:\s*["\']([^"\']+)["\']', re.M | re.DOTALL)
_KAFKA_JS_CONS   = re.compile(r'consumer\.subscribe\s*\(\s*\{[^}]*topic\s*:\s*["\']([^"\']+)["\']', re.M | re.DOTALL)

# -- RabbitMQ --
_RABBIT_PY_PUB   = re.compile(r'channel\.basic_publish\s*\([^)]*routing_key\s*=\s*["\']([^"\']+)["\']', re.M | re.DOTALL)
_RABBIT_PY_CONS  = re.compile(r'channel\.basic_consume\s*\([^)]*queue\s*=\s*["\']([^"\']+)["\']', re.M | re.DOTALL)
_RABBIT_JAVA     = re.compile(r'@RabbitListener\s*\(\s*queues\s*=\s*["\{]([^"}\)]+)["\}]', re.M)

# -- Redis --
_REDIS_PUB  = re.compile(r'(?:redis|r|client)\.publish\s*\(\s*["\']([^"\']+)["\']', re.M)
_REDIS_SUB  = re.compile(r'(?:pubsub|ps)\.subscribe\s*\(\s*["\']([^"\']+)["\']', re.M)

# -- SQS / SNS --
_SQS_SEND   = re.compile(r'sqs\.send_message\s*\([^)]*QueueUrl\s*=\s*["\']([^"\']+)["\']', re.M | re.DOTALL)
_SNS_PUB    = re.compile(r'sns\.publish\s*\([^)]*TopicArn\s*=\s*["\']([^"\']+)["\']', re.M | re.DOTALL)
_SQS_RECV   = re.compile(r'sqs\.receive_message\s*\([^)]*QueueUrl\s*=\s*["\']([^"\']+)["\']', re.M | re.DOTALL)

# -- NATS --
_NATS_PUB  = re.compile(r'(?:nc|nats|js)\.publish\s*\(\s*["\']([^"\']+)["\']', re.M)
_NATS_SUB  = re.compile(r'(?:nc|nats|js)\.subscribe\s*\(\s*["\']([^"\']+)["\']', re.M)

_SPEC_NAMES = frozenset({
    "openapi.yaml", "openapi.json", "openapi.yml",
    "swagger.yaml", "swagger.json", "swagger.yml",
    "asyncapi.yaml", "asyncapi.json", "asyncapi.yml",
})
_ENTRYPOINTS = frozenset({
    "app.py", "main.py", "server.py", "application.py",
    "app.ts", "main.ts", "server.ts", "index.ts",
    "App.java", "Application.java",
    "main.go", "server.go",
})


def resolve_events(files: list, project_path, extraction: dict) -> list:
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    file_strs = [str(f).replace("\\", "/") for f in files]

    edges = []
    node_by_file: dict[str, list] = defaultdict(list)
    root_posix = Path(str(project_path)).as_posix()
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if sf:
            node_by_file[sf].append(n)
            if not sf.startswith("/") and not (len(sf) > 1 and sf[1] == ":"):
                node_by_file[f"{root_posix}/{sf}"].append(n)

    contents: dict[str, str] = {}
    for f in file_strs:
        try:
            contents[f] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # topic_key -> {"publishers": [file], "subscribers": [file]}
    topic_map: dict[str, dict] = defaultdict(lambda: {"publishers": [], "subscribers": []})

    for f, c in contents.items():
        _scan_kafka(f, c, topic_map)
        _scan_rabbit(f, c, topic_map)
        _scan_redis(f, c, topic_map)
        _scan_sqs_sns(f, c, topic_map)
        _scan_nats(f, c, topic_map)

    for topic_key, participants in topic_map.items():
        broker, topic_name = topic_key.split(":", 1)
        conf = _broker_confidence(broker)
        pub_files = list(set(participants["publishers"]))
        sub_files = list(set(participants["subscribers"]))

        for pub_file in pub_files:
            pub_nodes = node_by_file.get(pub_file, [])
            if pub_nodes:
                edges.append(_event_edge(
                    pub_nodes[0]["id"], f"__topic__{topic_key}",
                    pub_file, f"__topic__/{topic_name}",
                    f"{broker}_publish", conf, topic_name, broker,
                ))

        for sub_file in sub_files:
            sub_nodes = node_by_file.get(sub_file, [])
            if sub_nodes:
                edges.append(_event_edge(
                    f"__topic__{topic_key}", sub_nodes[0]["id"],
                    f"__topic__/{topic_name}", sub_file,
                    f"{broker}_subscribe", conf, topic_name, broker,
                ))

        # Direct publisher -> subscriber cross-service edge
        for pf in pub_files:
            for sf in sub_files:
                if pf == sf:
                    continue
                pnodes = node_by_file.get(pf, [])
                snodes = node_by_file.get(sf, [])
                if pnodes and snodes:
                    edges.append(_event_edge(
                        pnodes[0]["id"], snodes[0]["id"],
                        pf, sf, "event_channel", 0.75, topic_name, broker,
                    ))

    # OpenAPI / AsyncAPI spec -> entrypoint
    spec_files = [f for f in file_strs if Path(f).name in _SPEC_NAMES]
    entrypoint_files = [f for f in file_strs if Path(f).name in _ENTRYPOINTS]
    for spec in spec_files:
        spec_nodes = node_by_file.get(spec, [])
        if not spec_nodes:
            continue
        is_async = "asyncapi" in Path(spec).name
        spec_dir = Path(spec).parent.as_posix()
        spec_parent = Path(spec).parent.parent.as_posix()
        nearby = [
            f for f in entrypoint_files
            if str(Path(f).parent).replace("\\", "/") in (spec_dir, spec_parent)
        ]
        for ep in nearby:
            for tn in node_by_file.get(ep, []):
                etype = "asyncapi_impl" if is_async else "openapi_impl"
                edges.append(_edge_plain(spec_nodes[0]["id"], tn["id"],
                                         spec, ep, etype, 0.85))

    return edges


def _scan_kafka(f, c, tm):
    for t in _KAFKA_PY_SEND.findall(c):   tm[f"kafka:{t}"]["publishers"].append(f)
    for t in _KAFKA_JAVA_SEND.findall(c): tm[f"kafka:{t}"]["publishers"].append(f)
    for t in _KAFKA_JS_PROD.findall(c):   tm[f"kafka:{t}"]["publishers"].append(f)
    for ts in _KAFKA_PY_SUB.findall(c):
        for t in re.findall(r"['\"]([^'\"]+)['\"]", ts):
            tm[f"kafka:{t}"]["subscribers"].append(f)
    for t in _KAFKA_JAVA_LIST.findall(c): tm[f"kafka:{t.strip()}"]["subscribers"].append(f)
    for t in _KAFKA_JS_CONS.findall(c):   tm[f"kafka:{t}"]["subscribers"].append(f)

def _scan_rabbit(f, c, tm):
    for t in _RABBIT_PY_PUB.findall(c):  tm[f"rabbitmq:{t}"]["publishers"].append(f)
    for t in _RABBIT_PY_CONS.findall(c): tm[f"rabbitmq:{t}"]["subscribers"].append(f)
    for t in _RABBIT_JAVA.findall(c):    tm[f"rabbitmq:{t.strip()}"]["subscribers"].append(f)

def _scan_redis(f, c, tm):
    for t in _REDIS_PUB.findall(c): tm[f"redis:{t}"]["publishers"].append(f)
    for t in _REDIS_SUB.findall(c): tm[f"redis:{t}"]["subscribers"].append(f)

def _scan_sqs_sns(f, c, tm):
    for url in _SQS_SEND.findall(c):
        tm[f"sqs:{url.rstrip('/').split('/')[-1]}"]["publishers"].append(f)
    for arn in _SNS_PUB.findall(c):
        tm[f"sns:{arn.rsplit(':', 1)[-1]}"]["publishers"].append(f)
    for url in _SQS_RECV.findall(c):
        tm[f"sqs:{url.rstrip('/').split('/')[-1]}"]["subscribers"].append(f)

def _scan_nats(f, c, tm):
    for t in _NATS_PUB.findall(c): tm[f"nats:{t}"]["publishers"].append(f)
    for t in _NATS_SUB.findall(c): tm[f"nats:{t}"]["subscribers"].append(f)

def _broker_confidence(broker: str) -> float:
    return {"kafka": 0.80, "rabbitmq": 0.75, "redis": 0.70,
            "sqs": 0.75, "sns": 0.75, "nats": 0.70}.get(broker, 0.65)

def _event_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, topic, broker) -> dict:
    return {
        "source": src_id, "target": tgt_id,
        "source_file": src_file, "target_file": tgt_file,
        "relation": etype, "type": etype, "confidence": confidence,
        "topic": topic, "broker": broker,
        "resolver": "event", "fix_confidence_delta": 0.0, "resolution_weight": 0.0,
    }

def _edge_plain(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return {
        "source": src_id, "target": tgt_id,
        "source_file": src_file, "target_file": tgt_file,
        "relation": etype, "type": etype, "confidence": confidence,
        "resolver": "event", "fix_confidence_delta": 0.0, "resolution_weight": 0.0,
    }
