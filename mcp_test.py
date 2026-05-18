"""
Drives icx mcp run via stdin/stdout JSON-RPC - exactly as Cursor/Windsurf does.
Tests build_codebase_graph for F:\clients\vil and reports what gets stuck.
"""
import json
import subprocess
import sys
import threading
import time

PROJECT_PATH = r"F:\clients\vil"
MCP_CMD = ["icx", "mcp", "run"]

proc = subprocess.Popen(
    MCP_CMD,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

_id = 0
def next_id():
    global _id
    _id += 1
    return _id

def send(msg: dict):
    line = json.dumps(msg)
    proc.stdin.write(line + "\n")
    proc.stdin.flush()

def recv(timeout=10.0) -> dict | None:
    """Read one line from stdout with timeout."""
    result = [None]
    def _read():
        try:
            result[0] = proc.stdout.readline()
        except Exception:
            pass
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if result[0]:
        try:
            return json.loads(result[0])
        except Exception:
            return {"raw": result[0].strip()}
    return None

def call(method: str, params: dict, timeout=120.0) -> dict:
    rid = next_id()
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    t0 = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - t0
        if elapsed > timeout:
            return {"error": f"TIMEOUT after {elapsed:.0f}s"}
        remaining = timeout - elapsed
        msg = recv(timeout=min(remaining, 5.0))
        if msg is None:
            print(f"  ... waiting ({elapsed:.0f}s elapsed)")
            continue
        if "raw" in msg:
            print(f"  raw: {msg['raw'][:120]}")
            continue
        if msg.get("id") == rid:
            return msg
        # notification or other message - print and keep waiting
        if "method" in msg:
            print(f"  notification: {msg['method']}")

# ---- Handshake ----
print("Initializing MCP...")
t0 = time.perf_counter()
r = call("initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "1.0"},
})
print(f"  elapsed: {time.perf_counter()-t0:.2f}s")
if "error" in r and "TIMEOUT" in str(r.get("error","")):
    print(f"STUCK: MCP server never responded to initialize: {r}")
    proc.kill()
    sys.exit(1)
print(f"  server: {r.get('result',{}).get('serverInfo',{})}")

send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

# ---- List tools ----
print("\nListing tools...")
r = call("tools/list", {}, timeout=10)
tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
print(f"  tools: {tools}")

# ---- analyze_issue_fast ----
print("\nStep 1: analyze_issue_fast VILMA-1873...")
t0 = time.perf_counter()
r = call("tools/call", {
    "name": "analyze_issue_fast",
    "arguments": {"issue_ref": "VILMA-1873"},
}, timeout=60)
elapsed = time.perf_counter() - t0
if "TIMEOUT" in str(r.get("error", "")):
    print(f"  STUCK: analyze_issue_fast timed out after {elapsed:.0f}s")
else:
    content = r.get("result", {}).get("content", [{}])[0].get("text", "{}")
    data = json.loads(content)
    if "error" in data:
        print(f"  ERROR: {data['error']}")
    else:
        key = "problem_summary" if "problem_summary" in data else "summary"
        print(f"  OK ({elapsed:.1f}s) - {str(data.get(key,''))[:80]}")

# ---- search_memory ----
print("\nStep 2: search_memory (timing cold start)...")
t0 = time.perf_counter()
r = call("tools/call", {
    "name": "search_memory",
    "arguments": {"query": "campaign status scheduled display N/A"},
}, timeout=120)
elapsed = time.perf_counter() - t0
if "TIMEOUT" in str(r.get("error", "")):
    print(f"  STUCK: search_memory timed out after {elapsed:.0f}s - ONNX cold load exceeded timeout")
else:
    content = r.get("result", {}).get("content", [{}])[0].get("text", "{}")
    data = json.loads(content)
    if "error" in data:
        print(f"  ERROR ({elapsed:.1f}s): {data['error']}")
    else:
        print(f"  OK ({elapsed:.1f}s) - count={data.get('count')}")

# ---- build_codebase_graph ----
print(f"\nStep 3: build_codebase_graph {PROJECT_PATH} (force=true)...")
print("  (watching for hangs - will wait up to 5 minutes)")
t0 = time.perf_counter()
r = call("tools/call", {
    "name": "build_codebase_graph",
    "arguments": {"project_path": PROJECT_PATH, "force": True},
}, timeout=300)
elapsed = time.perf_counter() - t0
if "TIMEOUT" in str(r.get("error", "")):
    print(f"  STUCK: build_codebase_graph hung for {elapsed:.0f}s")
    print("  This is likely ProcessPoolExecutor spawn overhead on Windows")
    print("  or graphify extraction hanging on a specific file type")
else:
    content = r.get("result", {}).get("content", [{}])[0].get("text", "{}")
    data = json.loads(content)
    if "error" in data:
        print(f"  ERROR ({elapsed:.1f}s): {data['error']}")
    else:
        print(f"  OK ({elapsed:.1f}s) - status={data.get('status')} files={data.get('file_count')}")

proc.kill()
print("\nDone.")
