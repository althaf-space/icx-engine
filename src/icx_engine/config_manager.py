from __future__ import annotations
import base64
import contextlib
import json
import os
import random
import re
import stat
import sys
import threading
import time
from pathlib import Path

from icx_engine.models.config import AppConfig, BaseConnection

CONFIG_PATH = Path.home() / ".icx" / "config.json"
_WARNED_PATH = CONFIG_PATH.parent / ".warned_plaintext"
_SERVICE = "icx"
_SENTINEL = "__keychain__"
_HEALTH_KEY = "_icx_healthcheck_"

_MASTER_KEY_ACCOUNT = "icx_master_key"
_DLOCK_PREFIX       = "dlock:v1:"
_DLOCK_THRESHOLD    = 512  # bytes - Windows keyring credential size limit

_LOCK_TIMEOUT = 10.0
_LOCK_RETRY_BASE = 0.050   # 50 ms initial backoff
_LOCK_RETRY_MAX  = 1.0     # cap at 1 s
_thread_lock = threading.Lock()  # in-process guard: threads share a PID so file-lock stale detection can't distinguish them


def _keyring_available() -> bool:
    try:
        import keyring
        keyring.set_password(_SERVICE, _HEALTH_KEY, "ok")
        result = keyring.get_password(_SERVICE, _HEALTH_KEY)
        try:
            keyring.delete_password(_SERVICE, _HEALTH_KEY)
        except Exception:
            pass
        return result == "ok"
    except Exception:
        return False


_keychain_ok: bool | None = None


def _check_keychain() -> bool:
    global _keychain_ok
    if _keychain_ok is None:
        _keychain_ok = _keyring_available()
    return _keychain_ok


def _warned_accounts() -> set[str]:
    try:
        return set(_WARNED_PATH.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return set()


def _mark_warned(account: str) -> None:
    warned = _warned_accounts()
    warned.add(account)
    try:
        _WARNED_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _WARNED_PATH.write_text("\n".join(sorted(warned)), encoding="utf-8")
    except Exception:
        pass


def _warn_plaintext(account: str, label: str) -> None:
    """Plaintext storage warning — fires once per account, never again."""
    if account in _warned_accounts():
        return
    env_var = _env_key(account)
    print(
        f"Warning: keyring unavailable - {label} stored as plaintext "
        f"in {CONFIG_PATH} (mode 0600).\n"
        f"  Set {env_var}=<value> to avoid plaintext storage.",
        file=sys.stderr,
    )
    _mark_warned(account)


def _warn_oauth_plaintext(field: str, acct_prefix: str, domain: str) -> None:
    _warn_plaintext(f"{acct_prefix}:{domain}", f"OAuth '{field}' for {domain}")


def _kset(account: str, value: str) -> bool:
    """Store value in keychain. Returns True on success, False if keychain rejects it."""
    try:
        import keyring
        keyring.set_password(_SERVICE, account, value)
        return True
    except Exception:
        return False


def _kget(account: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(_SERVICE, account)
    except Exception:
        return None


def _kdel(account: str) -> None:
    import keyring
    try:
        keyring.delete_password(_SERVICE, account)
    except Exception:
        pass


def _get_or_create_master_key() -> bytes:
    """Return the 32-byte D-Lock Master Key, generating and storing it on first call."""
    hex_key = _kget(_MASTER_KEY_ACCOUNT)
    if not hex_key:
        key = os.urandom(32)
        if not _kset(_MASTER_KEY_ACCOUNT, key.hex()):
            from icx_engine.exceptions import ConfigError
            raise ConfigError(
                "D-Lock setup failed: the OS keyring rejected the Master Key write. "
                "The keyring appeared healthy during startup but refused the store — "
                "this can happen when the keyring is locked or storage quota is exceeded. "
                "Unlock your OS keyring and retry."
            )
        return key
    return bytes.fromhex(hex_key)


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
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True   # process exists but we have no permission to signal it
    except OSError:
        return False


@contextlib.contextmanager
def _config_lock():
    """
    Cross-platform advisory exclusive lock on config.json.lock.

    Spins with exponential back-off (± 10 % jitter) until the lock is
    acquired or _LOCK_TIMEOUT seconds elapse.  The lock is always released
    in a try/finally block - even if the caller raises.

    Unix: fcntl.flock - the OS automatically reclaims the lock on process death.
    Windows: O_CREAT|O_EXCL atomic creation; stale locks are evicted by PID check.
    """
    lock_path = CONFIG_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

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
                        owner_pid = int(lock_path.read_text().strip())
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

    jira_token:example.atlassian.net  →  ICX_JIRA_TOKEN_EXAMPLE_ATLASSIAN_NET
    llm_api_key                        →  ICX_LLM_API_KEY
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
        if not CONFIG_PATH.exists():
            return AppConfig()
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            from icx_engine.exceptions import ConfigError
            raise ConfigError(
                f"Failed to read config file at {CONFIG_PATH}: {exc}. "
                "The file may be corrupted. Delete it to start fresh."
            ) from exc

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
                    auth["api_token"] = _dlock_decrypt(token_val)
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
                        auth[field_name] = _dlock_decrypt(val)
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
                        ch["api_key"] = _dlock_decrypt(api_key_val)

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
                "(example.atlassian.net → EXAMPLE_ATLASSIAN_NET).",
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

        # 0o700: only owner can list/enter the config directory (macOS/Linux)
        CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # PID-unique staging file: concurrent processes get their own clean room
        # and can never clobber each other during the serialization phase.
        tmp = CONFIG_PATH.parent / f"{CONFIG_PATH.name}.tmp.{os.getpid()}"

        with _config_lock():
            # Lock is held - write the staging file then atomically replace.
            # Create temp file with 0o600 from the start - eliminates the TOCTOU
            # window between write and chmod on shared machines.
            try:
                fd = os.open(
                    str(tmp),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    stat.S_IRUSR | stat.S_IWUSR,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(raw, indent=2))
            except OSError:
                # Fallback for platforms where os.open mode is ignored (e.g. Windows)
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

    @staticmethod
    def delete_all_secrets(config: AppConfig) -> None:
        """Remove all keychain entries for the given config before clearing it."""
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
