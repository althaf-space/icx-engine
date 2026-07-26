"""Native security testing - deterministic, no external scanner, no extra installer.

Static analysis over the repo's own source (secrets, SAST-lite, dependency/SCA) plus the runtime DAST
probes woven into the UI/API flow (see analyzers/security_cases.py). All findings are severity-graded and
folded into the run result + the human HTML report. Honest ceiling: real-AST rule matching (not full
taint flow) and offline/manifest dependency checks (not a live CVE feed) - clearly labelled as such.
"""
from __future__ import annotations

from icx_engine.testing.security.aggregate import (
    Finding,
    fold_into_result,
    run_static_security,
)

__all__ = ["Finding", "run_static_security", "fold_into_result"]
