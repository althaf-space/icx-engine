from __future__ import annotations


def validate_session_args(args: dict) -> tuple[bool, str]:
    fp = args.get("file_paths")
    if not isinstance(fp, list) or not fp or not all(isinstance(x, str) and x for x in fp):
        return False, "file_paths must be a non-empty list of strings"
    if args.get("test_mode") not in ("automated", "manual"):
        return False, "test_mode must be 'automated' or 'manual'"
    mi = args.get("max_iterations")
    if mi is not None and (not isinstance(mi, int) or mi < 1):
        return False, "max_iterations must be a positive integer"
    return True, ""


def validate_login_args(args: dict, required: list[str]) -> tuple[bool, str]:
    for key in required:
        val = args.get(key)
        if not isinstance(val, str) or not val:
            return False, f"{key} is required and must be a non-empty string"
    return True, ""
