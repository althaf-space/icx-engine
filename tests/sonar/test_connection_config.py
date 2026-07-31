import icx_engine.config_manager as cm
from icx_engine.config_manager import ConfigManager
from icx_engine.models.config import AppConfig, GitLabConnection, SonarConnection


def test_sonar_connection_token_roundtrip_and_excluded(isolated_config, monkeypatch):
    # Pin a deterministic in-memory keychain so the test exercises the secure
    # path (sentinel on disk, token in keychain) on every platform. Headless CI
    # has no OS keyring, where save() would otherwise fall back to intentional
    # plaintext storage and this exclusion assertion would spuriously fail.
    store: dict[str, str] = {}
    monkeypatch.setattr(cm, "_check_keychain", lambda: True)
    monkeypatch.setattr(cm, "_kset", lambda account, value: store.__setitem__(account, value) or True)
    monkeypatch.setattr(cm, "_kget", lambda account: store.get(account))
    monkeypatch.setattr(cm, "_kdel", lambda account: store.pop(account, None))

    cfg = AppConfig()
    cfg.sonar_connections = {
        "prod": SonarConnection(name="prod", url="http://prod:9000", token="secret-token-123"),
    }
    cfg.active_sonar = "prod"
    ConfigManager.save(cfg)

    # token must never be written to the plaintext config file
    disk = isolated_config.read_text(encoding="utf-8")
    assert "secret-token-123" not in disk

    loaded = ConfigManager.load()
    assert loaded.active_sonar == "prod"
    assert loaded.sonar_connections["prod"].url == "http://prod:9000"
    assert loaded.sonar_connections["prod"].token == "secret-token-123"


def test_sonar_connection_model_excludes_token():
    sc = SonarConnection(name="x", url="http://x:9000", token="t")
    assert "token" not in sc.model_dump()


def test_sonar_remove_by_index_resolution(isolated_config):
    from icx_engine.cli import _sonar_resolve_name
    cfg = AppConfig()
    cfg.sonar_connections = {
        "alpha": SonarConnection(name="alpha", url="http://a:9000", token="t"),
        "beta": SonarConnection(name="beta", url="http://b:9000", token="t"),
    }
    cfg.active_sonar = "alpha"
    ConfigManager.save(cfg)
    assert _sonar_resolve_name("2") == "beta"     # index -> name
    assert _sonar_resolve_name("alpha") == "alpha"  # name passthrough
    assert _sonar_resolve_name("9") == "9"          # out of range -> passthrough (errors downstream)


def test_delete_all_secrets_clears_every_sonar_account(monkeypatch):
    # logout / uninstall must remove every Sonar keyring account - legacy + per-connection
    deleted = []
    monkeypatch.setattr(cm, "_check_keychain", lambda: True)
    monkeypatch.setattr(cm, "_kdel", lambda account: deleted.append(account))
    cfg = AppConfig()
    cfg.sonar_connections = {
        "prod": SonarConnection(name="prod", url="http://p:9000", token="t1"),
        "staging": SonarConnection(name="staging", url="http://s:9000", token="t2"),
    }
    cfg.active_sonar = "prod"
    ConfigManager.delete_all_secrets(cfg)
    assert "sonar_token" in deleted                 # legacy account
    assert "sonar_conn_token:prod" in deleted        # per-connection accounts
    assert "sonar_conn_token:staging" in deleted


def test_remove_connection_deletes_its_keyring_secret(monkeypatch):
    from icx_engine.sonar import service
    deleted = []
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    monkeypatch.setattr(ConfigManager, "delete_sonar_connection_secret",
                        staticmethod(lambda name: deleted.append(name)))
    cfg = AppConfig()
    cfg.sonar_connections = {"prod": SonarConnection(name="prod", url="http://p:9000", token="t")}
    cfg.active_sonar = "prod"
    service.remove_connection("prod", cfg=cfg)
    assert deleted == ["prod"]                        # keyring token cleared on remove


def test_save_clears_legacy_sonar_token_orphan(isolated_config, monkeypatch):
    # after migration config.sonar_token is empty -> save must purge the old keyring entry
    deleted = []
    monkeypatch.setattr(cm, "_check_keychain", lambda: True)
    monkeypatch.setattr(cm, "_kdel", lambda account: deleted.append(account))
    monkeypatch.setattr(cm, "_kset", lambda a, v: True)
    cfg = AppConfig()
    cfg.sonar_connections = {"prod": SonarConnection(name="prod", url="http://p:9000", token="t")}
    cfg.active_sonar = "prod"
    ConfigManager.save(cfg)
    assert "sonar_token" in deleted


def test_legacy_config_loads_as_connection(isolated_config):
    # simulate an OLD config file (single-server, pre-connections) and confirm it
    # becomes a visible, removable connection after load
    import json
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps({
        "sonar_url": "http://legacy:9000",
        "sonar_enabled": True,
    }), encoding="utf-8")
    loaded = ConfigManager.load()
    assert "default" in loaded.sonar_connections
    assert loaded.active_sonar == "default"
    assert loaded.sonar_connections["default"].url == "http://legacy:9000"


# --- GitLab connection (mirrors the Sonar connection tests above) ---

def test_gitlab_connection_model_excludes_token():
    gc = GitLabConnection(name="x", url="http://x.gitlab.com", token="t")
    assert "token" not in gc.model_dump()


def test_app_config_active_gitlab_connection_resolves_named_entry():
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    config = AppConfig(gitlab_connections={"gitlab.example.com": conn}, active_gitlab="gitlab.example.com")
    assert config.active_gitlab_connection() == conn


def test_app_config_active_gitlab_connection_none_when_unset():
    config = AppConfig()
    assert config.active_gitlab_connection() is None


def test_gitlab_connection_token_roundtrip_and_excluded(isolated_config, monkeypatch):
    # Pin a deterministic in-memory keychain so the test exercises the secure
    # path (sentinel on disk, token in keychain) on every platform. Headless CI
    # has no OS keyring, where save() would otherwise fall back to intentional
    # plaintext storage and this exclusion assertion would spuriously fail.
    store: dict[str, str] = {}
    monkeypatch.setattr(cm, "_check_keychain", lambda: True)
    monkeypatch.setattr(cm, "_kset", lambda account, value: store.__setitem__(account, value) or True)
    monkeypatch.setattr(cm, "_kget", lambda account: store.get(account))
    monkeypatch.setattr(cm, "_kdel", lambda account: store.pop(account, None))

    cfg = AppConfig()
    cfg.gitlab_connections = {
        "gitlab.example.com": GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-secret"),
    }
    cfg.active_gitlab = "gitlab.example.com"
    ConfigManager.save(cfg)

    # token must never be written to the plaintext config file
    disk = isolated_config.read_text(encoding="utf-8")
    assert "glpat-secret" not in disk

    loaded = ConfigManager.load()
    assert loaded.active_gitlab == "gitlab.example.com"
    assert loaded.gitlab_connections["gitlab.example.com"].url == "https://gitlab.example.com"
    assert loaded.gitlab_connections["gitlab.example.com"].token == "glpat-secret"


def test_config_manager_saves_and_loads_gitlab_token_via_plaintext_fallback(isolated_config, monkeypatch):
    # deterministic plaintext-fallback path (no OS keyring available in CI)
    monkeypatch.setattr(cm, "_check_keychain", lambda: False)
    cfg = AppConfig(gitlab_connections={
        "gitlab.example.com": GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-secret"),
    }, active_gitlab="gitlab.example.com")
    ConfigManager.save(cfg)
    reloaded = ConfigManager.load()
    assert reloaded.gitlab_connections["gitlab.example.com"].token == "glpat-secret"


def test_config_manager_delete_gitlab_connection_secret_is_a_noop_without_keychain(monkeypatch):
    monkeypatch.setattr(cm, "_check_keychain", lambda: False)
    ConfigManager.delete_gitlab_connection_secret("gitlab.example.com")  # must not raise


def test_delete_all_secrets_clears_gitlab_connection_accounts(monkeypatch):
    deleted = []
    monkeypatch.setattr(cm, "_check_keychain", lambda: True)
    monkeypatch.setattr(cm, "_kdel", lambda account: deleted.append(account))
    cfg = AppConfig()
    cfg.gitlab_connections = {
        "gitlab.example.com": GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="t1"),
    }
    cfg.active_gitlab = "gitlab.example.com"
    ConfigManager.delete_all_secrets(cfg)
    assert "gitlab_conn_token:gitlab.example.com" in deleted
