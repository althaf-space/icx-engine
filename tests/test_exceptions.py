"""ICXMemoryError rename + back-compat alias (Phase A / finding C1)."""
import builtins

from icx_engine.exceptions import ICXError, ICXMemoryError, MemoryError


def test_icx_memory_error_is_icx_error():
    assert issubclass(ICXMemoryError, ICXError)


def test_memory_error_alias_points_to_icx_memory_error():
    # Back-compat: existing `from icx_engine.exceptions import MemoryError` keeps working.
    assert MemoryError is ICXMemoryError


def test_icx_memory_error_does_not_shadow_builtin():
    # The custom error must not be, or derive from, the builtin MemoryError (OOM).
    assert ICXMemoryError is not builtins.MemoryError
    assert builtins.MemoryError not in ICXMemoryError.__mro__


def test_icx_memory_error_message_preserved():
    err = ICXMemoryError("storage path not writable")
    assert str(err) == "storage path not writable"
