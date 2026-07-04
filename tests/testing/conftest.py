import pytest

from icx_engine.testing import rules as _rules


@pytest.fixture(autouse=True)
def _isolate_testing_rules(tmp_path_factory, monkeypatch):
    """Redirect the rulebook dir to a temp path so tests never read or write the
    real ~/.icx/testing_rules. Bundled defaults still seed the temp dir."""
    d = tmp_path_factory.mktemp("icx_rules")
    monkeypatch.setattr(_rules, "rules_dir", lambda: d)
    return d
