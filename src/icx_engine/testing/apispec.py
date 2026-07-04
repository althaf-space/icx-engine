from __future__ import annotations

import re
from dataclasses import dataclass

_SPRING_RE = re.compile(r'@(Get|Post|Put|Patch|Delete)Mapping(?:\(\s*(?:value\s*=\s*)?"([^"]*)")?', re.IGNORECASE)
_SPRING_BASE_RE = re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*)?"([^"]*)"')
_PY_RE = re.compile(r'@(?:app|router)\.(get|post|put|patch|delete)\(\s*"([^"]*)"', re.IGNORECASE)
_FLASK_RE = re.compile(r'@app\.route\(\s*"([^"]*)"(?:.*methods\s*=\s*\[\s*"([A-Z]+)")?', re.IGNORECASE)


@dataclass
class Endpoint:
    method: str
    path: str
    sample_body: dict | None = None


def extract_endpoint(file_path: str, content: str) -> Endpoint | None:
    m = _PY_RE.search(content)
    if m:
        return Endpoint(method=m.group(1).upper(), path=m.group(2), sample_body=None)
    m = _SPRING_RE.search(content)
    if m:
        base = _SPRING_BASE_RE.search(content)
        base_path = base.group(1) if base else ""
        sub = m.group(2) or ""
        path = (base_path.rstrip("/") + "/" + sub.lstrip("/")).rstrip("/") if sub else base_path
        return Endpoint(method=m.group(1).upper(), path=path or "/", sample_body=None)
    m = _FLASK_RE.search(content)
    if m:
        return Endpoint(method=(m.group(2) or "GET").upper(), path=m.group(1), sample_body=None)
    return None


def build_request_spec(base_url: str, ep: Endpoint, headers: dict | None = None) -> dict:
    url = base_url.rstrip("/") + "/" + ep.path.lstrip("/")
    spec: dict = {"url": url, "method": ep.method, "headers": headers or {}}
    if ep.sample_body is not None:
        spec["body"] = ep.sample_body
    return spec
