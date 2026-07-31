from __future__ import annotations
from icx_engine.confirm import issue_token, verify_token


def test_verify_returns_payload_for_valid_token():
    token = issue_token("commit", {"message": "ABC-1 fix"})
    result = verify_token(token, "commit")
    assert result == {"message": "ABC-1 fix"}


def test_verify_returns_none_for_unknown_token():
    assert verify_token("not-a-real-token", "commit") is None


def test_verify_returns_none_for_wrong_action():
    token = issue_token("commit", {"message": "ABC-1 fix"})
    assert verify_token(token, "push") is None


def test_token_is_single_use():
    token = issue_token("commit", {"message": "ABC-1 fix"})
    assert verify_token(token, "commit") is not None
    assert verify_token(token, "commit") is None


def test_tokens_are_unique_per_call():
    t1 = issue_token("commit", {"message": "a"})
    t2 = issue_token("commit", {"message": "b"})
    assert t1 != t2


def test_pending_store_is_bounded_oldest_evicted_first(monkeypatch):
    import icx_engine.confirm as confirm_mod
    monkeypatch.setattr(confirm_mod, "_PENDING", {})
    monkeypatch.setattr(confirm_mod, "_MAX_PENDING", 3)

    tokens = [issue_token("commit", {"i": i}) for i in range(5)]

    # The two oldest were evicted to stay within the cap.
    assert verify_token(tokens[0], "commit") is None
    assert verify_token(tokens[1], "commit") is None
    # The three most recent survive.
    assert verify_token(tokens[2], "commit") == {"i": 2}
    assert verify_token(tokens[3], "commit") == {"i": 3}
    assert verify_token(tokens[4], "commit") == {"i": 4}


def test_pending_store_never_exceeds_cap(monkeypatch):
    import icx_engine.confirm as confirm_mod
    monkeypatch.setattr(confirm_mod, "_PENDING", {})
    monkeypatch.setattr(confirm_mod, "_MAX_PENDING", 3)

    for i in range(10):
        issue_token("commit", {"i": i})
    assert len(confirm_mod._PENDING) == 3
