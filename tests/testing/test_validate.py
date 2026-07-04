from icx_engine.testing.validate import validate_session_args, validate_login_args


def test_session_args_ok():
    ok, msg = validate_session_args({"file_paths": ["a.tsx"], "test_mode": "automated"})
    assert ok is True and msg == ""


def test_session_args_empty_files():
    ok, msg = validate_session_args({"file_paths": [], "test_mode": "automated"})
    assert ok is False and "file_paths" in msg


def test_session_args_bad_mode():
    ok, msg = validate_session_args({"file_paths": ["a"], "test_mode": "x"})
    assert ok is False and "test_mode" in msg


def test_session_args_bad_max_iter():
    ok, msg = validate_session_args({"file_paths": ["a"], "test_mode": "automated", "max_iterations": 0})
    assert ok is False and "max_iterations" in msg


def test_login_args_required():
    ok, msg = validate_login_args({"loginUrl": "http://x"}, ["loginUrl", "username", "password"])
    assert ok is False and ("username" in msg or "password" in msg)
    ok2, _ = validate_login_args({"loginUrl": "http://x", "username": "u", "password": "p"},
                                 ["loginUrl", "username", "password"])
    assert ok2 is True
