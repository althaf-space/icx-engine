from __future__ import annotations
import base64
import contextlib
import json
import logging
import os
import random
import re
import stat
import sys
import threading
import time
from pathlib import Path

from icx_engine.models.config import AppConfig, BaseConnection

_log = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".icx" / "config.json"
_SERVICE = "icx"


def _icx_dir() -> Path:
    return Path.home() / ".icx"


# Runtime files that OLDER ICX versions created but the current version no longer uses. Cleaned on
# `icx update` and MCP startup so every upgrading user ends up without stale artifacts - guarded, so
# a missing file or a permission error is never fatal. Add a relative path here when a feature that
# wrote a user-side file is removed.
_STALE_ARTIFACTS: tuple[str, ...] = (
    "testing_rules/profile_gen.md",   # the profile_gen gate was removed with the Magik retirement
)

# Config keys OLDER versions wrote that the current model no longer defines. They are dropped from
# disk automatically the next time config is saved (save() rebuilds JSON from the model), but we
# detect + report them on `icx update` so removal is explicit. Add a key here when a config field
# is retired.
_STALE_CONFIG_KEYS: tuple[str, ...] = (
    "agent_max_steps",          # retired: deterministic replay has no browser-step budget
    "magik_base_url", "magik_api_key", "magik_max_iterations",
    "magik_use_streaming", "magik_agent_max_steps", "magik_agent_step_cap",
)


def stale_config_keys_on_disk() -> list[str]:
    """Return the retired config keys currently present in ~/.icx/config.json (before a save strips
    them). Guarded - empty list when the file is absent or unreadable."""
    try:
        import json as _json
        raw = _json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [k for k in _STALE_CONFIG_KEYS if isinstance(raw, dict) and k in raw]


def clean_stale_artifacts() -> list[str]:
    """Delete known-stale runtime files under ~/.icx. Idempotent + guarded. Returns the paths removed.

    Only touches ICX-owned files that a prior version created and the current version does not use -
    never user data (config, memory, sessions, captured logins). Safe to call repeatedly."""
    removed: list[str] = []
    base = _icx_dir()
    for rel in _STALE_ARTIFACTS:
        p = base / rel
        try:
            if p.is_file():
                p.unlink()
                removed.append(str(p))
        except OSError:
            pass
    return removed
_SENTINEL = "__keychain__"
_HEALTH_KEY = "_icx_healthcheck_"

_MASTER_KEY_ACCOUNT = "icx_master_key"
_DLOCK_PREFIX       = "dlock:v1:"
_DLOCK_THRESHOLD    = 512  # bytes - Windows keyring credential size limit
_MASTER_KEY_FILE    = CONFIG_PATH.parent / ".master_key"

_master_key_cache: bytes | None = None
_master_key_lock = threading.Lock()

_LOCK_TIMEOUT = 10.0
_LOCK_RETRY_BASE = 0.050   # 50 ms initial backoff
_LOCK_RETRY_MAX  = 1.0     # cap at 1 s
_thread_lock = threading.Lock()  # in-process guard: threads share a PID so file-lock stale detection can't distinguish them


# ---------------------------------------------------------------------------
# DPAPI-protected master key file (Windows only)
#
# Windows Credential Manager uses DPAPI internally for the same user-bound
# encryption. Storing the master key in a DPAPI blob on disk is identical in
# security to storing it in the keyring -- both require the same user on the
# same machine to decrypt. This lets background MCP processes (which may not
# reach the Credential Manager service) still get the real master key.
#
# On macOS/Linux the keyring is reliably accessible from background processes,
# so no file cache is needed there.
# ---------------------------------------------------------------------------

def _dpapi_protect(data: bytes) -> bytes:
    """Encrypt bytes with Windows DPAPI (current user, no UI). Raises OSError on failure."""
    import ctypes
    import ctypes.wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data)
    blob_in = _BLOB(len(data), buf)
    blob_out = _BLOB()
    # 0x01 = CRYPTPROTECT_UI_FORBIDDEN: fail instead of prompting for UI
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out),
    ):
        raise OSError(f"CryptProtectData failed (error {ctypes.GetLastError()})")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt a DPAPI blob (current user, no UI). Raises OSError on failure."""
    import ctypes
    import ctypes.wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data)
    blob_in = _BLOB(len(data), buf)
    blob_out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out),
    ):
        raise OSError(f"CryptUnprotectData failed (error {ctypes.GetLastError()})")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _write_master_key_file(key: bytes) -> None:
    """Persist a DPAPI-encrypted copy of the master key. No-op on non-Windows."""
    if sys.platform != "win32":
        return
    try:
        protected = _dpapi_protect(key)
        _MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MASTER_KEY_FILE.with_suffix(".tmp")
        tmp.write_bytes(protected)
        tmp.replace(_MASTER_KEY_FILE)
    except Exception as exc:
        _log.warning("Could not write master key cache: %s", exc)


def _read_master_key_file() -> bytes | None:
    """Read and DPAPI-decrypt the cached master key. Returns None if absent or unreadable."""
    if sys.platform != "win32":
        return None
    try:
        return _dpapi_unprotect(_MASTER_KEY_FILE.read_bytes())
    except FileNotFoundError:
        return None
    except Exception as exc:
        _log.warning("Could not read master key cache: %s", exc)
        return None


def _keyring_call(fn, timeout: float = 3.0):
    """Run fn() in a daemon thread with hard timeout. Returns (result, timed_out).
    Circuit-breaker: on timeout disables keychain for this process and returns (None, True).
    Prevents Windows Credential Manager from blocking in sandboxed MCP processes.
    """
    global _keychain_ok
    _result: list = [None]
    _exc: list = [None]

    def _run() -> None:
        try:
            _result[0] = fn()
        except Exception as e:
            _exc[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _keychain_ok = False
        _log.warning("keyring call timed out after %.0fs; falling back to env-var lookup", timeout)
        return None, True
    if _exc[0] is not None:
        raise _exc[0]
    return _result[0], False


def _keyring_available() -> bool:
    try:
        import keyring as _kr
        _, timed_out = _keyring_call(lambda: _kr.get_password(_SERVICE, _HEALTH_KEY))
        return not timed_out
    except Exception:
        return False


_keychain_ok: bool | None = None
_keychain_init_lock = threading.Lock()


def _check_keychain() -> bool:
    global _keychain_ok
    if _keychain_ok is None:
        with _keychain_init_lock:
            if _keychain_ok is None:
                _keychain_ok = _keyring_available()
    return _keychain_ok


def _warned_accounts() -> set[str]:
    try:
        return set((CONFIG_PATH.parent / ".warned_plaintext").read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return set()


def _mark_warned(account: str) -> None:
    warned_path = CONFIG_PATH.parent / ".warned_plaintext"
    warned = _warned_accounts()
    warned.add(account)
    try:
        warned_path.parent.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
        warned_path.write_text("\n".join(sorted(warned)), encoding="utf-8")
    except Exception:
        pass


def _warn_plaintext(account: str, label: str) -> None:
    """Plaintext storage warning - fires once per account, never again."""
    if account in _warned_accounts():
        return
    # The exact ICX_* environment-variable name is intentionally NOT echoed here.
    # It is a non-secret identifier, but logging any account-derived string trips
    # clear-text-secret scanners; the concrete variable-name scheme is printed
    # once by warn_if_plaintext(), so this per-account notice stays generic.
    print(
        f"Warning: keyring unavailable - {label} stored as plaintext "
        f"in {CONFIG_PATH} (mode 0600).\n"
        f"  Set the matching ICX_ environment variable to avoid plaintext storage.",
        file=sys.stderr,
    )
    _mark_warned(account)


def _warn_oauth_plaintext(field: str, acct_prefix: str, domain: str) -> None:
    _warn_plaintext(f"{acct_prefix}:{domain}", f"OAuth '{field}' for {domain}")


def _kset(account: str, value: str) -> bool:
    """Store value in keychain. Returns True on success, False if keychain rejects it or times out."""
    try:
        import keyring as _kr
        _, timed_out = _keyring_call(lambda: _kr.set_password(_SERVICE, account, value))
        return not timed_out
    except Exception:
        return False


def _kget(account: str) -> str | None:
    try:
        import keyring as _kr
        result, timed_out = _keyring_call(lambda: _kr.get_password(_SERVICE, account))
        return None if timed_out else result
    except Exception:
        return None


def _kdel(account: str) -> None:
    try:
        import keyring as _kr
        _keyring_call(lambda: _kr.delete_password(_SERVICE, account))
    except Exception:
        pass


def _get_or_create_master_key() -> bytes:
    """Return the 32-byte D-Lock Master Key.

    Priority order:
    1. In-memory cache (fastest, same process).
    2. OS keyring (authoritative store). On success, refreshes the DPAPI file cache.
    3. DPAPI-encrypted file (~/.icx/.master_key) -- used when keyring is blocked in
       background processes (e.g. MCP servers). Same user-bound security as the keyring.
    4. Ephemeral random key (last resort -- existing D-Lock values will fail to decrypt).
    """
    global _master_key_cache
    with _master_key_lock:
        if _master_key_cache is not None:
            return _master_key_cache

        if _check_keychain():
            hex_key = _kget(_MASTER_KEY_ACCOUNT)
            if hex_key:
                try:
                    key = bytes.fromhex(hex_key)
                    _master_key_cache = key
                    _write_master_key_file(key)  # keep DPAPI cache in sync
                    return key
                except ValueError:
                    print(
                        "[config] D-Lock Master Key in keyring is not valid hex; "
                        "using a temporary in-memory key. Re-authenticate with `icx connect` to fix.",
                        file=sys.stderr,
                    )
                    # fall through to DPAPI file then ephemeral
            else:
                # Fresh system or first-time setup: generate and persist.
                key = os.urandom(32)
                if not _kset(_MASTER_KEY_ACCOUNT, key.hex()):
                    print(
                        "[config] D-Lock Master Key could not be stored in the keyring; "
                        "using a temporary in-memory key for this session.",
                        file=sys.stderr,
                    )
                _write_master_key_file(key)
                _master_key_cache = key
                return key

        # Keyring unavailable (timed out or blocked): try DPAPI file cache.
        cached = _read_master_key_file()
        if cached is not None:
            _master_key_cache = cached
            return cached

        # No file cache yet (user has never run the CLI on this machine).
        # Ephemeral key -- existing D-Lock values cannot be decrypted.
        key = os.urandom(32)
        _master_key_cache = key
        return key


def _dlock_encrypt(value: str) -> str:
    """AES-256-GCM encrypt value and return a tagged base64 string safe for config.json."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key   = _get_or_create_master_key()
    nonce = os.urandom(12)
    ct    = AESGCM(key).encrypt(nonce, value.encode(), None)
    return _DLOCK_PREFIX + base64.b64encode(nonce + ct).decode()


def _dlock_decrypt(tagged: str) -> str:
    """Decrypt a dlock:v1: tagged string. Raises ConfigError on tamper or key mismatch."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    from icx_engine.exceptions import ConfigError
    try:
        key = _get_or_create_master_key()
        raw = base64.b64decode(tagged[len(_DLOCK_PREFIX):], validate=True)
        return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()
    except (InvalidTag, ValueError) as exc:
        raise ConfigError(
            "D-Lock decryption failed. The Master Key may have been reset or the config "
            "file tampered with. Re-authenticate with `icx connect` or `icx model --add`."
        ) from exc


def _llm_text_account(profile_name: str) -> str:
    return f"llm_text:{profile_name}"


def _llm_image_account(profile_name: str) -> str:
    return f"llm_image:{profile_name}"


def _pid_exists(pid: int) -> bool:
    """Return True if a process with the given PID appears to be running."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        # psutil unavailable - fall back to Unix-only signal check
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False


@contextlib.contextmanager
def _config_lock():
    """
    Cross-platform advisory exclusive lock on config.json.lock.

    Spins with exponential back-off (+/- 10 % jitter) until the lock is
    acquired or _LOCK_TIMEOUT seconds elapse.  The lock is always released
    in a try/finally block - even if the caller raises.

    Unix: fcntl.flock - the OS automatically reclaims the lock on process death.
    Windows: O_CREAT|O_EXCL atomic creation; stale locks are evicted by PID check.
    """
    lock_path = CONFIG_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))

    # _thread_lock serialises threads within the same process first; the file
    # lock below then serialises independent processes.
    with _thread_lock:
        if sys.platform != "win32":
            import fcntl  # noqa: PLC0415
            lf = open(lock_path, "a")  # noqa: WPS515 - create-or-open, not truncate
            try:
                deadline = time.monotonic() + _LOCK_TIMEOUT
                delay    = _LOCK_RETRY_BASE
                while True:
                    try:
                        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Could not acquire config lock within {_LOCK_TIMEOUT}s. "
                                f"Remove {lock_path} if no other icx process is running."
                            )
                        time.sleep(min(delay + random.uniform(0, delay * 0.1), _LOCK_RETRY_MAX))
                        delay = min(delay * 2, _LOCK_RETRY_MAX)
                yield
            finally:
                try:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                except Exception:
                    pass
                lf.close()
                try:
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            # Windows: atomic file-creation (O_EXCL) as the mutual-exclusion primitive.
            # Write our PID so a later process can evict us if we crash without cleanup.
            pid      = os.getpid()
            deadline = time.monotonic() + _LOCK_TIMEOUT
            delay    = _LOCK_RETRY_BASE
            while True:
                try:
                    fd = os.open(
                        str(lock_path),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        stat.S_IRUSR | stat.S_IWUSR,
                    )
                    os.write(fd, str(pid).encode())
                    os.close(fd)
                    break
                except FileExistsError:
                    try:
                        raw_pid = lock_path.read_text(encoding="utf-8").strip()
                        owner_pid = int(raw_pid)
                        if not _pid_exists(owner_pid):
                            lock_path.unlink(missing_ok=True)
                            try:
                                fd = os.open(
                                    str(lock_path),
                                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                    stat.S_IRUSR | stat.S_IWUSR,
                                )
                                os.write(fd, str(pid).encode())
                                os.close(fd)
                                break  # acquired
                            except FileExistsError:
                                pass  # another process won the race; fall through to backoff
                    except ValueError:
                        try:
                            lock_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    except Exception:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Could not acquire config lock within {_LOCK_TIMEOUT}s. "
                            f"Remove {lock_path} if no other icx process is running."
                        )
                    time.sleep(min(delay + random.uniform(0, delay * 0.1), _LOCK_RETRY_MAX))
                    delay = min(delay * 2, _LOCK_RETRY_MAX)
            try:
                yield
            finally:
                try:
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass


def _env_key(account: str) -> str:
    """Map a keyring account name to an environment variable name.

    jira_token:example.atlassian.net  ->  ICX_JIRA_TOKEN_EXAMPLE_ATLASSIAN_NET
    llm_api_key                        ->  ICX_LLM_API_KEY
    """
    sanitized = re.sub(r"[^A-Za-z0-9]", "_", account).upper()
    return f"ICX_{sanitized}"


def _env_get(account: str) -> str | None:
    """Read a secret from the environment (headless / CI fallback)."""
    return os.environ.get(_env_key(account)) or None


def _resolve(account: str) -> str | None:
    """Resolve a secret: OS keyring first, environment-variable fallback.

    The env-var path lets CI pipelines and headless Linux servers supply
    credentials without a GUI keyring daemon.  Keyring always takes
    precedence when it is available and returns a non-empty value.
    """
    if _check_keychain():
        val = _kget(account)
        if val:
            return val
    return _env_get(account)


class ConfigManager:
    @staticmethod
    def load() -> AppConfig:
        _last_perm_exc: Exception | None = None
        for _attempt in range(4):
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                break
            except FileNotFoundError:
                return AppConfig()
            except PermissionError as exc:
                _last_perm_exc = exc
                if sys.platform == "win32" and _attempt == 0:
                    # Self-heal: bad DACL from old os.open(0o600) - reset to inherited ACLs.
                    try:
                        import subprocess
                        subprocess.run(
                            ["icacls", str(CONFIG_PATH), "/reset", "/C"],
                            capture_output=True,
                            check=False,
                        )
                    except Exception:
                        pass
                time.sleep(0.05 * (2 ** _attempt))  # 50ms -> 100ms -> 200ms -> 400ms
            except (OSError, UnicodeDecodeError) as exc:
                from icx_engine.exceptions import ConfigError
                raise ConfigError(
                    f"Failed to read config file at {CONFIG_PATH}: {exc}"
                ) from exc
            except json.JSONDecodeError as exc:
                from icx_engine.exceptions import ConfigError
                raise ConfigError(
                    f"Config file at {CONFIG_PATH} is corrupted (invalid JSON). "
                    "Delete it to start fresh: it will be recreated on next `icx connection --add`."
                ) from exc
        else:
            from icx_engine.exceptions import ConfigError
            raise ConfigError(
                f"Permission denied reading config file at {CONFIG_PATH}. "
                "Another program may be locking it. On Windows, run: "
                f'icacls "{CONFIG_PATH}" /grant %USERNAME%:F'
            ) from _last_perm_exc

        # True when any secret is stored as plain text in config.json (pre-keyring config).
        # Triggers a save() at the end to migrate those secrets into the OS keyring.
        needs_secret_migration = False

        for conn in raw.get("connections", []):
            ctype = conn.get("connector_type", "")
            auth = conn.get("auth", {})
            domain = conn.get("domain", "")
            if auth.get("auth_type") == "token":
                token_val = auth.get("api_token", "")
                if token_val == _SENTINEL:
                    auth["api_token"] = _resolve(f"{ctype}_token:{domain}") or ""
                elif token_val.startswith(_DLOCK_PREFIX):
                    try:
                        auth["api_token"] = _dlock_decrypt(token_val)
                    except Exception as _exc:
                        _fallback = _env_get(f"{ctype}_token:{domain}")
                        if _fallback:
                            _log.debug("[config] D-Lock decrypt failed for %s token (%s); using env var.", ctype, domain)
                            auth["api_token"] = _fallback
                        else:
                            from icx_engine.exceptions import ConfigError
                            raise ConfigError(
                                f"D-Lock decryption failed for {ctype} token ({domain}). "
                                "The keyring master key is unavailable (blocked in background process). "
                                "Re-authenticate with `icx connect` or set the credential via environment variable."
                            ) from _exc
                elif token_val:
                    needs_secret_migration = True
            elif auth.get("auth_type") == "oauth":
                for field_name, acct_prefix in (
                    ("access_token", "oauth_access"),
                    ("refresh_token", "oauth_refresh"),
                    ("client_secret", "oauth_secret"),
                ):
                    val = auth.get(field_name) or ""
                    if val == _SENTINEL:
                        auth[field_name] = _resolve(f"{acct_prefix}:{domain}") or ""
                    elif val.startswith(_DLOCK_PREFIX):
                        try:
                            auth[field_name] = _dlock_decrypt(val)
                        except Exception as _exc:
                            _fallback = _env_get(f"{acct_prefix}:{domain}")
                            if _fallback:
                                _log.debug("[config] D-Lock decrypt failed for OAuth %s (%s); using env var.", field_name, domain)
                                auth[field_name] = _fallback
                            else:
                                from icx_engine.exceptions import ConfigError
                                raise ConfigError(
                                    f"D-Lock decryption failed for OAuth {field_name} ({domain}). "
                                    "The keyring master key is unavailable (blocked in background process). "
                                    "Re-authenticate with `icx connect` or set the credential via environment variable."
                                ) from _exc
                    elif val:
                        needs_secret_migration = True

        # Resolve llm_profiles channel sentinels
        for profile_name, profile in raw.get("llm_profiles", {}).items():
            for channel, acct_fn in (
                ("text_config", _llm_text_account),
                ("image_config", _llm_image_account),
            ):
                ch = profile.get(channel)
                if isinstance(ch, dict):
                    api_key_val = ch.get("api_key", "")
                    if api_key_val == _SENTINEL:
                        ch["api_key"] = _resolve(acct_fn(profile_name)) or ""
                    elif api_key_val.startswith(_DLOCK_PREFIX):
                        try:
                            ch["api_key"] = _dlock_decrypt(api_key_val)
                        except Exception as _exc:
                            _fallback = _env_get(acct_fn(profile_name))
                            if _fallback:
                                _log.debug("[config] D-Lock decrypt failed for LLM profile '%s' (%s); using env var.", profile_name, channel)
                                ch["api_key"] = _fallback
                            else:
                                from icx_engine.exceptions import ConfigError
                                raise ConfigError(
                                    f"D-Lock decryption failed for LLM profile '{profile_name}' ({channel}). "
                                    "The keyring master key is unavailable (blocked in background process). "
                                    "Re-authenticate with `icx model --add` or set the credential via environment variable."
                                ) from _exc

        # Resolve sonar_token sentinel
        sonar_raw = raw.get("sonar_token", "")
        if sonar_raw == _SENTINEL:
            raw["sonar_token"] = _resolve("sonar_token") or None
        elif sonar_raw and isinstance(sonar_raw, str) and sonar_raw.startswith(_DLOCK_PREFIX):
            try:
                raw["sonar_token"] = _dlock_decrypt(sonar_raw)
            except Exception:
                raw["sonar_token"] = _env_get("sonar_token") or None
        elif sonar_raw:
            needs_secret_migration = True

        # Resolve per-connection Sonar tokens
        for _sc_name, _sc in (raw.get("sonar_connections") or {}).items():
            if not isinstance(_sc, dict):
                continue
            _sc_acct = f"sonar_conn_token:{_sc_name}"
            _sc_tok = _sc.get("token") or ""
            if _sc_tok == _SENTINEL:
                _sc["token"] = _resolve(_sc_acct) or None
            elif isinstance(_sc_tok, str) and _sc_tok.startswith(_DLOCK_PREFIX):
                try:
                    _sc["token"] = _dlock_decrypt(_sc_tok)
                except Exception:
                    _sc["token"] = _env_get(_sc_acct) or None
            elif _sc_tok:
                needs_secret_migration = True

        # Resolve secret fields for registered third-party integrations.
        # No-op for configs without an "integrations" map (i.e. every existing config).
        from icx_engine.integrations import integration_secret_fields  # noqa: PLC0415
        for _int_name, _int_data in (raw.get("integrations") or {}).items():
            if not isinstance(_int_data, dict):
                continue
            for _field in integration_secret_fields(_int_name):
                _acct = f"integration_secret:{_int_name}:{_field}"
                _val = _int_data.get(_field) or ""
                if _val == _SENTINEL:
                    _int_data[_field] = _resolve(_acct) or ""
                elif isinstance(_val, str) and _val.startswith(_DLOCK_PREFIX):
                    try:
                        _int_data[_field] = _dlock_decrypt(_val)
                    except Exception:
                        _int_data[_field] = _env_get(_acct) or ""
                elif _val:
                    needs_secret_migration = True

        config = AppConfig.model_validate(raw)

        if needs_secret_migration and _check_keychain():
            ConfigManager.save(config)

        return config

    @staticmethod
    def warn_if_plaintext() -> None:
        """Show plaintext env-var reference once per machine. Never repeats after first display."""
        if not _check_keychain():
            if "__summary__" in _warned_accounts():
                return
            _mark_warned("__summary__")
            print(
                "Warning: system keyring unavailable - credentials stored in "
                f"{CONFIG_PATH} (mode 0600).\n"
                "Use environment variables to avoid plaintext storage:\n"
                "  Jira token auth:  ICX_JIRA_TOKEN_<DOMAIN>        "
                "e.g. ICX_JIRA_TOKEN_EXAMPLE_ATLASSIAN_NET\n"
                "  OAuth access:     ICX_OAUTH_ACCESS_<DOMAIN>       "
                "e.g. ICX_OAUTH_ACCESS_EXAMPLE_ATLASSIAN_NET\n"
                "  OAuth refresh:    ICX_OAUTH_REFRESH_<DOMAIN>      "
                "e.g. ICX_OAUTH_REFRESH_EXAMPLE_ATLASSIAN_NET\n"
                "  OAuth secret:     ICX_OAUTH_SECRET_<DOMAIN>       "
                "e.g. ICX_OAUTH_SECRET_EXAMPLE_ATLASSIAN_NET\n"
                "  LLM text channel: ICX_LLM_TEXT_<PROFILE>          "
                "e.g. ICX_LLM_TEXT_DEFAULT\n"
                "  LLM image channel:ICX_LLM_IMAGE_<PROFILE>         "
                "e.g. ICX_LLM_IMAGE_DEFAULT\n"
                "Replace dots/hyphens in domain or profile name with underscores, uppercase "
                "(example.atlassian.net -> EXAMPLE_ATLASSIAN_NET).",
                file=sys.stderr,
            )

    @staticmethod
    def save(config: AppConfig) -> None:
        raw = json.loads(config.model_dump_json())

        # Secret fields are excluded from model_dump_json() via Field(exclude=True).
        # Re-inject them from the live model objects, then store in keychain (replacing
        # with sentinel) or write as plaintext when keychain is unavailable.
        conn_list = raw.get("connections", [])
        for i, conn_model in enumerate(config.connections):
            if i >= len(conn_list):
                continue
            auth_model = getattr(conn_model, "auth", None)
            auth_raw = conn_list[i].get("auth")
            if auth_model is None or auth_raw is None:
                continue
            domain = conn_model.domain
            ctype = conn_model.connector_type
            auth_type = getattr(auth_model, "auth_type", "")

            if auth_type == "token":
                token = getattr(auth_model, "api_token", "") or ""
                if token:
                    if _check_keychain() and len(token) > _DLOCK_THRESHOLD:
                        auth_raw["api_token"] = _dlock_encrypt(token)
                    elif _check_keychain() and _kset(f"{ctype}_token:{domain}", token):
                        auth_raw["api_token"] = _SENTINEL
                    else:
                        auth_raw["api_token"] = token
                        _warn_plaintext(f"{ctype}_token:{domain}", f"Jira API token for {domain}")
            elif auth_type == "oauth":
                for attr, acct_prefix in (
                    ("access_token", "oauth_access"),
                    ("refresh_token", "oauth_refresh"),
                    ("client_secret", "oauth_secret"),
                ):
                    val = getattr(auth_model, attr, None) or ""
                    if val:
                        if _check_keychain() and len(val) > _DLOCK_THRESHOLD:
                            auth_raw[attr] = _dlock_encrypt(val)
                        elif _check_keychain() and _kset(f"{acct_prefix}:{domain}", val):
                            auth_raw[attr] = _SENTINEL
                        else:
                            auth_raw[attr] = val
                            _warn_oauth_plaintext(attr, acct_prefix, domain)

        for profile_name, profile_model in config.llm_profiles.items():
            if profile_name not in raw.get("llm_profiles", {}):
                continue
            profile_raw = raw["llm_profiles"][profile_name]
            for channel_attr, acct_fn in (
                ("text_config", _llm_text_account),
                ("image_config", _llm_image_account),
            ):
                ch_model = getattr(profile_model, channel_attr, None)
                ch_raw = profile_raw.get(channel_attr)
                if ch_model is None or not isinstance(ch_raw, dict):
                    continue
                api_key = getattr(ch_model, "api_key", None) or ""
                if api_key:
                    acct = acct_fn(profile_name)
                    if _check_keychain() and len(api_key) > _DLOCK_THRESHOLD:
                        ch_raw["api_key"] = _dlock_encrypt(api_key)
                    elif _check_keychain() and _kset(acct, api_key):
                        ch_raw["api_key"] = _SENTINEL
                    else:
                        ch_raw["api_key"] = api_key
                        _channel_label = "text model" if channel_attr == "text_config" else "image model"
                        _warn_plaintext(acct, f"LLM API key for profile '{profile_name}' ({_channel_label})")

        # Store sonar_token via keyring (excluded from Pydantic serialization).
        # New code never sets the legacy field (it is migrated into a connection
        # on load), so the else-branch clears any stale legacy keyring entry so no
        # duplicate token lingers after migration.
        sonar_token = config.sonar_token
        if sonar_token:
            if _check_keychain() and _kset("sonar_token", sonar_token):
                raw["sonar_token"] = _SENTINEL
            else:
                raw["sonar_token"] = sonar_token
                _warn_plaintext("sonar_token", "Sonar token")
        else:
            _kdel("sonar_token")

        # Store per-connection Sonar tokens via keyring (token is excluded from
        # Pydantic serialization, so the resolved value is added back here).
        for _sc_name, _sc_model in config.sonar_connections.items():
            if _sc_name not in raw.get("sonar_connections", {}):
                continue
            _sc_raw = raw["sonar_connections"][_sc_name]
            _sc_tok = getattr(_sc_model, "token", None) or ""
            if not _sc_tok:
                continue
            _sc_acct = f"sonar_conn_token:{_sc_name}"
            if _check_keychain() and len(_sc_tok) > _DLOCK_THRESHOLD:
                _sc_raw["token"] = _dlock_encrypt(_sc_tok)
            elif _check_keychain() and _kset(_sc_acct, _sc_tok):
                _sc_raw["token"] = _SENTINEL
            else:
                _sc_raw["token"] = _sc_tok
                _warn_plaintext(_sc_acct, f"Sonar token for connection '{_sc_name}'")

        # Store secret fields for registered integrations (generic; excluded
        # from plaintext serialization). No-op when no integrations are stored.
        from icx_engine.integrations import integration_secret_fields  # noqa: PLC0415
        for _int_name, _int_data in (raw.get("integrations") or {}).items():
            if not isinstance(_int_data, dict):
                continue
            for _field in integration_secret_fields(_int_name):
                _val = _int_data.get(_field) or ""
                if not _val:
                    continue
                _acct = f"integration_secret:{_int_name}:{_field}"
                if _check_keychain() and len(_val) > _DLOCK_THRESHOLD:
                    _int_data[_field] = _dlock_encrypt(_val)
                elif _check_keychain() and _kset(_acct, _val):
                    _int_data[_field] = _SENTINEL
                else:
                    _int_data[_field] = _val
                    _warn_plaintext(_acct, f"{_int_name} {_field}")

        # 0o700: owner-only on macOS/Linux; Windows user-profile isolation makes this unnecessary
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
        # PID-unique staging file: concurrent processes get their own clean room
        # and can never clobber each other during the serialization phase.
        tmp = CONFIG_PATH.parent / f"{CONFIG_PATH.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}"

        with _config_lock():
            # Lock is held - write the staging file then atomically replace.
            if sys.platform == "win32":
                tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            else:
                try:
                    fd = os.open(
                        str(tmp),
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                        stat.S_IRUSR | stat.S_IWUSR,
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(raw, indent=2))
                except OSError:
                    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
                    try:
                        tmp.chmod(0o600)
                    except Exception:
                        pass
            try:
                tmp.replace(CONFIG_PATH)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            if sys.platform == "win32":
                # Reset any inherited ACL restrictions so all Windows security
                # contexts (e.g. MCP server spawned by an AI editor) can read it.
                try:
                    import subprocess
                    subprocess.run(
                        ["icacls", str(CONFIG_PATH), "/reset", "/C"],
                        capture_output=True,
                        check=False,
                    )
                except Exception:
                    pass

    @staticmethod
    def delete_all_secrets(config: AppConfig) -> None:
        """Remove all keychain entries for the given config before clearing it."""
        global _master_key_cache
        if not _check_keychain():
            return
        for conn in config.connections:
            ctype = conn.connector_type
            domain = conn.domain
            _kdel(f"{ctype}_token:{domain}")
            _kdel(f"oauth_access:{domain}")
            _kdel(f"oauth_refresh:{domain}")
            _kdel(f"oauth_secret:{domain}")
        for profile_name in config.llm_profiles:
            _kdel(_llm_text_account(profile_name))
            _kdel(_llm_image_account(profile_name))
        _kdel("sonar_token")
        for _sc_name in (config.sonar_connections or {}):
            _kdel(f"sonar_conn_token:{_sc_name}")
        # Registered integration secrets.
        from icx_engine.integrations import integration_secret_fields  # noqa: PLC0415
        for _int_name in (config.integrations or {}):
            for _field in integration_secret_fields(_int_name):
                _kdel(f"integration_secret:{_int_name}:{_field}")
        # Delete the D-Lock master key from both keyring and DPAPI file cache.
        _kdel(_MASTER_KEY_ACCOUNT)
        try:
            if _MASTER_KEY_FILE.exists():
                _MASTER_KEY_FILE.unlink()
        except Exception:
            pass
        _master_key_cache = None

    @staticmethod
    def delete_connection_secrets(conn: BaseConnection) -> None:
        if not _check_keychain():
            return
        ctype = conn.connector_type
        domain = conn.domain
        _kdel(f"{ctype}_token:{domain}")
        _kdel(f"oauth_access:{domain}")
        _kdel(f"oauth_refresh:{domain}")
        _kdel(f"oauth_secret:{domain}")

    @staticmethod
    def delete_sonar_connection_secret(name: str) -> None:
        if not _check_keychain():
            return
        _kdel(f"sonar_conn_token:{name}")

    @staticmethod
    def delete_llm_profile_secrets(profile_name: str) -> None:
        if not _check_keychain():
            return
        _kdel(_llm_text_account(profile_name))
        _kdel(_llm_image_account(profile_name))

    @staticmethod
    def delete_llm_image_secrets(profile_name: str) -> None:
        if not _check_keychain():
            return
        _kdel(_llm_image_account(profile_name))
