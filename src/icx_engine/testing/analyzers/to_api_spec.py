"""Convert a backend Element Census (endpoints + error codes) into executable API tests.

Two artifacts, materialized into `<repo>/.icx-apispec/`:
  - openapi.json  -> Schemathesis fuzzes the whole surface from it (deterministic, no AI).
  - *.hurl        -> one scripted request per endpoint asserting its documented success status.

Pure functions (`census_to_openapi`, `census_to_hurl`) + `materialize_api_spec` (writes files).
Guarded: unknown/missing keys degrade to sensible defaults; never raises on a partial census.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_PATH_TOKEN = re.compile(r"\{([^}]+)\}")


def _path_param_names(ep: dict) -> list[str]:
    """Every {token} in the path - the census's pathParams list may under-report them, and a
    templated path segment with no matching parameter makes the OpenAPI doc invalid (schemathesis
    can reject the WHOLE run) and leaves an unsubstituted {id} in the hurl request. So we treat the
    path itself as the source of truth for which path params must exist."""
    return _PATH_TOKEN.findall(str(ep.get("path", "")))

_TYPE_MAP = {
    "str": "string", "string": "string", "text": "string", "uuid": "string", "date": "string",
    "datetime": "string", "email": "string",
    "int": "integer", "integer": "integer", "long": "integer",
    "float": "number", "double": "number", "number": "number", "decimal": "number",
    "bool": "boolean", "boolean": "boolean",
    "list": "array", "array": "array",
    "dict": "object", "object": "object", "json": "object",
}


def _oa_type(t: object) -> str:
    return _TYPE_MAP.get(str(t or "").strip().lower(), "string")


def _endpoints(model: dict) -> list[dict]:
    eps = model.get("endpoints") if isinstance(model, dict) else None
    return [e for e in eps if isinstance(e, dict)] if isinstance(eps, list) else []


def _norm_path(path: str) -> str:
    return path if path.startswith("/") else "/" + path


def census_to_openapi(model: dict, base_url: str = "", title: str = "API Test Spec") -> dict:
    """Synthesize a minimal OpenAPI 3 doc from the census endpoints. Schemathesis runs off this."""
    paths: dict[str, dict] = {}
    for ep in _endpoints(model):
        method = str(ep.get("method", "get")).strip().lower() or "get"
        path = _norm_path(str(ep.get("path", "/")))
        params = []
        seen_path = set()
        for p in (ep.get("pathParams") or []):
            if isinstance(p, dict) and p.get("name"):
                seen_path.add(p["name"])
                params.append({"name": p["name"], "in": "path", "required": True,
                               "schema": {"type": _oa_type(p.get("type"))}})
        # synthesize any {token} in the path the census forgot to list (else the spec is invalid)
        for tok in _path_param_names(ep):
            if tok not in seen_path:
                seen_path.add(tok)
                params.append({"name": tok, "in": "path", "required": True,
                               "schema": {"type": "string"}})
        for q in (ep.get("queryParams") or []):
            if isinstance(q, dict) and q.get("name"):
                params.append({"name": q["name"], "in": "query", "required": bool(q.get("required")),
                               "schema": {"type": _oa_type(q.get("type"))}})
        for h in (ep.get("headers") or []):
            if isinstance(h, dict) and h.get("name"):
                params.append({"name": h["name"], "in": "header", "required": bool(h.get("required")),
                               "schema": {"type": "string"}})
        op: dict = {"operationId": str(ep.get("id") or f"{method}_{path}"), "parameters": params}

        body = ep.get("requestBody") or {}
        fields = body.get("fields") if isinstance(body, dict) else None
        if isinstance(fields, list) and fields:
            props, required = {}, []
            for f in fields:
                if not isinstance(f, dict) or not f.get("name"):
                    continue
                props[f["name"]] = {"type": _oa_type(f.get("type"))}
                if f.get("required"):
                    required.append(f["name"])
            schema = {"type": "object", "properties": props}
            if required:
                schema["required"] = required
            op["requestBody"] = {"required": True,
                                 "content": {"application/json": {"schema": schema}}}

        responses: dict[str, dict] = {}
        for sr in (ep.get("successResponses") or []):
            if isinstance(sr, dict) and sr.get("status"):
                responses[str(sr["status"])] = {"description": "success"}
        for er in (ep.get("errorCatalog") or []):
            if isinstance(er, dict) and er.get("status"):
                responses[str(er["status"])] = {"description": str(er.get("errorCode") or "error")}
        auth = ep.get("auth") or {}
        if isinstance(auth, dict) and auth.get("required"):
            responses.setdefault(str((auth.get("onFailure") or {}).get("status") or 401),
                                 {"description": "unauthorized"})
        if not responses:
            responses["200"] = {"description": "success"}
        op["responses"] = responses

        paths.setdefault(path, {})[method] = op

    doc: dict = {"openapi": "3.0.3", "info": {"title": title, "version": "1.0.0"}, "paths": paths}
    if base_url:
        doc["servers"] = [{"url": base_url}]
    return doc


def _example_for(field: dict) -> object:
    if not isinstance(field, dict):
        return "test"
    if field.get("happyExample") not in (None, ""):
        return field["happyExample"]
    t = _oa_type(field.get("type"))
    return {"integer": 1, "number": 1.0, "boolean": True, "array": [], "object": {}}.get(t, "sample")


def census_to_hurl(model: dict, base_var: str = "base") -> list[tuple[str, str]]:
    """One .hurl file per endpoint: request + assert its primary documented success status."""
    out: list[tuple[str, str]] = []
    for i, ep in enumerate(_endpoints(model)):
        method = str(ep.get("method", "GET")).strip().upper() or "GET"
        path = _norm_path(str(ep.get("path", "/")))
        # substitute EVERY {token} in the path (census pathParams may under-report them) so no
        # literal {id} is left in the request line - hurl uses {{var}}, not {id}, so an unsubstituted
        # token would 404 and be mis-reported as a product defect.
        path = _PATH_TOKEN.sub("1", path)
        lines = [f"{method} {{{{{base_var}}}}}{path}"]
        body = ep.get("requestBody") or {}
        fields = body.get("fields") if isinstance(body, dict) else None
        if isinstance(fields, list) and fields and method in ("POST", "PUT", "PATCH"):
            payload = {f["name"]: _example_for(f) for f in fields if isinstance(f, dict) and f.get("name")}
            lines.append("Content-Type: application/json")
            lines.append("```json")
            lines.append(json.dumps(payload, indent=2))
            lines.append("```")
        status = None
        for sr in (ep.get("successResponses") or []):
            if isinstance(sr, dict) and sr.get("status"):
                status = int(sr["status"]) if str(sr["status"]).isdigit() else sr["status"]
                break
        lines.append(f"HTTP {status if status is not None else 200}")
        name = f"{i:03d}_{method.lower()}_{path.strip('/').replace('/', '_') or 'root'}.hurl"
        out.append((name, "\n".join(lines) + "\n"))
    return out


def materialize_api_spec(model: dict, repo, base_url: str = "") -> dict:
    """Write openapi.json + *.hurl into <repo>/.icx-apispec/ so the api runners pick them up.
    Returns {openapi, hurl_dir, hurl_files, endpoints}. Guarded - returns {} on any failure."""
    try:
        eps = _endpoints(model)
        if not eps:
            return {}
        d = Path(repo) / ".icx-apispec"
        d.mkdir(parents=True, exist_ok=True)
        oa_path = d / "openapi.json"
        oa_path.write_text(json.dumps(census_to_openapi(model, base_url), indent=2), encoding="utf-8")
        hurl_files = []
        for name, content in census_to_hurl(model):
            fp = d / name
            fp.write_text(content, encoding="utf-8")
            hurl_files.append(str(fp))
        # SECURITY (always-on): full per-endpoint suite (injection classes, mass-assignment, auth) +
        # one app-wide response-header audit, woven into the same hurl run.
        from icx_engine.testing.analyzers.security_cases import api_security_requests, api_headers_check
        sec = 0
        for i, ep in enumerate(eps):
            for name, content in api_security_requests(ep):
                fp = d / f"{i:03d}_{name}"
                fp.write_text(content, encoding="utf-8")
                hurl_files.append(str(fp))
                sec += 1
        hname, hcontent = api_headers_check(str(eps[0].get("path", "/")))
        (d / hname).write_text(hcontent, encoding="utf-8")
        hurl_files.append(str(d / hname))
        sec += 1
        return {"openapi": str(oa_path), "hurl_dir": str(d),
                "hurl_files": hurl_files, "endpoints": len(eps), "security_cases": sec}
    except Exception:
        return {}
