"""JUnit XML -> normalized TestReport. The universal report spine.

Report XML is UNTRUSTED - it is produced by a runner in the user's repo. Parse it with defusedxml
so a malicious/broken report cannot mount an XXE, external-entity, or billion-laughs attack against
ICX. Falls back to stdlib only if defusedxml is unavailable (declared dependency; fallback keeps the
module importable in a partial env)."""
from __future__ import annotations

from pathlib import Path
from xml.etree.ElementTree import ParseError

try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
    from defusedxml.common import DefusedXmlException
    _XML_ERRORS: tuple[type[Exception], ...] = (ParseError, DefusedXmlException)
except ImportError:  # pragma: no cover - defusedxml is a declared dependency
    from xml.etree.ElementTree import fromstring as _xml_fromstring
    _XML_ERRORS = (ParseError,)

from icx_engine.testing.runners.base import TestCase, TestReport


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_junit_xml(source: str) -> TestReport:
    """Parse a JUnit XML string (or file path) into a normalized TestReport.

    Handles <testsuites> wrapping one or more <testsuite>, each with <testcase> children carrying
    optional <failure>/<error>/<skipped>. Counts are recomputed from the cases (not trusted from
    suite attributes) so the report is internally consistent.
    """
    text = source
    try:
        p = Path(source)
        if len(source) < 4096 and p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        text = source

    report = TestReport()
    try:
        root = _xml_fromstring(text)
    except _XML_ERRORS:
        return report

    suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
    for suite in suites:
        for tc in suite.iter("testcase"):
            case = TestCase(
                name=tc.get("name", ""),
                classname=tc.get("classname", ""),
                time=_to_float(tc.get("time")),
            )
            fail = tc.find("failure")
            err = tc.find("error")
            skip = tc.find("skipped")
            if err is not None:
                case.status = "error"
                case.message = err.get("message", "") or (err.text or "")
            elif fail is not None:
                case.status = "failed"
                case.message = fail.get("message", "") or (fail.text or "")
            elif skip is not None:
                case.status = "skipped"
                case.message = skip.get("message", "") or (skip.text or "")
            else:
                case.status = "passed"
            report.cases.append(case)

    report.total = len(report.cases)
    report.failures = sum(1 for c in report.cases if c.status == "failed")
    report.errors = sum(1 for c in report.cases if c.status == "error")
    report.skipped = sum(1 for c in report.cases if c.status == "skipped")
    report.passed = sum(1 for c in report.cases if c.status == "passed")
    report.time = round(sum(c.time for c in report.cases), 3)
    if isinstance(source, str) and source != text:
        report.raw_path = source
    return report
