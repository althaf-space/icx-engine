import re
from unittest.mock import patch

import click
from icx_engine.cli import app
from icx_engine.config_manager import ConfigManager
from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig


def test_help_exits_cleanly(cli_runner):
    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("connection", "model", "analyze", "status", "logout", "mcp", "graph", "test"):
        assert cmd in result.output


def test_full_help_lists_every_command():
    """Drift guard: EVERY registered command must appear in the hand-written `icx --help` text, so a
    new command can never be silently invisible. If this fails, add the command to _FULL_HELP."""
    import typer
    from icx_engine.cli import _FULL_HELP

    cli = typer.main.get_command(app)

    def walk(cmd, prefix=""):
        out = []
        if hasattr(cmd, "commands"):
            for name, sub in cmd.commands.items():
                out += walk(sub, (prefix + " " + name).strip())
        else:
            out.append(prefix)
        return out

    commands = walk(cli, "icx")
    missing = [c for c in commands if c not in _FULL_HELP and c[len("icx "):] not in _FULL_HELP]
    assert not missing, f"commands missing from `icx --help`: {missing}"


def test_every_leaf_command_has_debug_and_traceback_options():
    """Enforces developer.md Section 8's MANDATORY CLI convention: every leaf command must expose
    both --debug and --traceback. Walks the full command tree (including git/jira/gitlab/etc - every
    sub-app registered via app.add_typer) via typer.main.get_command(app), exactly as developer.md
    documents this test doing."""
    import typer

    cli = typer.main.get_command(app)

    def leaf_commands(cmd, prefix=""):
        out = []
        if hasattr(cmd, "commands"):
            for name, sub in cmd.commands.items():
                out += leaf_commands(sub, (prefix + " " + name).strip())
        else:
            out.append((prefix, cmd))
        return out

    leaves = leaf_commands(cli, "icx")
    missing = []
    for name, cmd in leaves:
        param_names = {p.name for p in cmd.params}
        if "debug" not in param_names or "traceback" not in param_names:
            missing.append(name)
    assert not missing, f"commands missing --debug/--traceback: {missing}"
    assert len(leaves) >= 70, (
        f"only found {len(leaves)} leaf commands - the walk itself may be broken "
        "(update this floor if commands were legitimately removed)"
    )


def test_graph_help(cli_runner):
    result = cli_runner.invoke(app, ["graph", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    for cmd in ("add", "build", "list", "status", "remove"):
        assert cmd in output


def test_git_help(cli_runner):
    result = cli_runner.invoke(app, ["git", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    assert "status" in output


def test_jira_help(cli_runner):
    result = cli_runner.invoke(app, ["jira", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    assert "update" in output


def test_graph_module_importable():
    from icx_engine.graph import GraphManager, generate_graph_report
    assert callable(GraphManager)
    assert callable(generate_graph_report)


def test_graph_manager_list_empty():
    from icx_engine.graph.manager import GraphManager
    from unittest.mock import patch
    with patch("icx_engine.graph.storage._graphs_root", lambda: __import__("pathlib").Path("/nonexistent/path/icx_test_xyz")):
        mgr = GraphManager()
        projects = mgr.list_projects()
        assert isinstance(projects, list)


def test_graph_error_is_icx_error():
    from icx_engine.exceptions import GraphError, ICXError
    assert issubclass(GraphError, ICXError)


def test_connection_help(cli_runner):
    result = cli_runner.invoke(app, ["connection", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    assert "--add" in output
    assert "--remove" in output
    assert "--active" in output


def test_model_help(cli_runner):
    result = cli_runner.invoke(app, ["model", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    assert "--add" in output
    assert "--active" in output
    assert "--remove" in output


def test_analyze_help(cli_runner):
    assert cli_runner.invoke(app, ["analyze", "--help"]).exit_code == 0


def test_mcp_help(cli_runner):
    result = cli_runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    for cmd in ("setup", "remove", "config", "run"):
        assert cmd in result.output


def test_test_rules_command(cli_runner, tmp_path, monkeypatch):
    from icx_engine.testing import rules as _rules
    monkeypatch.setattr(_rules, "rules_dir", lambda: tmp_path / "testing_rules")
    result = cli_runner.invoke(app, ["test", "rules"])
    assert result.exit_code == 0
    assert "2b.md" in result.output
    assert "compat_scan.md" in result.output


def test_test_rules_reset_reports_seeded_then_up_to_date(cli_runner, tmp_path, monkeypatch):
    from icx_engine.testing import rules as _rules
    monkeypatch.setattr(_rules, "rules_dir", lambda: tmp_path / "testing_rules")
    first = cli_runner.invoke(app, ["test", "rules", "--reset"])
    assert first.exit_code == 0
    assert "Seeded" in first.output and "compat_scan.md" in first.output

    second = cli_runner.invoke(app, ["test", "rules", "--reset"])
    assert second.exit_code == 0
    assert "Already current" in second.output


def test_test_rules_reset_leaves_customized_file_alone(cli_runner, tmp_path, monkeypatch):
    from icx_engine.testing import rules as _rules
    monkeypatch.setattr(_rules, "rules_dir", lambda: tmp_path / "testing_rules")
    d = tmp_path / "testing_rules"
    d.mkdir(parents=True)
    (d / "compat_scan.md").write_text("MY CUSTOM RULE", encoding="utf-8")
    result = cli_runner.invoke(app, ["test", "rules", "--reset"])
    assert result.exit_code == 0
    assert "Left alone" in result.output and "compat_scan.md" in result.output
    assert (d / "compat_scan.md").read_text(encoding="utf-8") == "MY CUSTOM RULE"


def test_status_runs_with_no_config(cli_runner):
    from unittest.mock import patch
    with patch.object(ConfigManager, "load", return_value=AppConfig()):
        result = cli_runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Connections" in result.output
    assert "AI Profiles" in result.output


def test_status_shows_indexed_connections(cli_runner):
    from unittest.mock import patch
    from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
    config = AppConfig(
        connections=[
            JiraConnection(domain="company.atlassian.net",
                           auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok")),
            JiraConnection(domain="personal.atlassian.net",
                           auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok")),
        ],
        default_connection="jira:company.atlassian.net",
    )
    with patch.object(ConfigManager, "load", return_value=config):
        result = cli_runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert re.search(r"(?<![.\w])company\.atlassian\.net(?![.\w])", result.output)
    assert "ACTIVE" in result.output
    assert "1" in result.output
    assert "2" in result.output


def test_status_shows_indexed_ai_profiles(cli_runner):
    from unittest.mock import patch
    config = AppConfig(
        llm_profiles={
            "personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3")),
            "work": LLMConfig(text_config=ChannelConfig(provider="openai", model="gpt-4o")),
        },
        current_llm_profile="work",
    )
    with patch.object(ConfigManager, "load", return_value=config):
        result = cli_runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # Profile names may be truncated by Rich when terminal width is narrow in tests.
    # Check for a prefix that survives truncation.
    assert "perso" in result.output  # "personal" or "perso..."
    assert "work" in result.output
    assert "ACTI" in result.output   # "[ACTIVE]" or "[ACTI..."


def test_analyze_requires_argument(cli_runner):
    assert cli_runner.invoke(app, ["analyze"]).exit_code != 0


def test_analyze_invalid_key_exits_cleanly(cli_runner):
    assert cli_runner.invoke(app, ["analyze", "notakey"]).exit_code != 0


def test_mcp_config_prints_snippet(cli_runner):
    result = cli_runner.invoke(app, ["mcp", "config"])
    assert result.exit_code == 0
    assert "mcpServers" in result.output
    assert "icx" in result.output


def test_raw_issue_data_has_phase4_fields():
    """RawIssueData carries optional fields with correct defaults."""
    from icx_engine.models.output import RawIssueData
    raw = RawIssueData(
        issue_key="P4-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="Medium", status="Open", metadata={},
    )
    assert raw.due_date is None
    assert raw.attachment_content_urls == {}
    assert raw.attachment_texts == {}


def test_compute_missing_is_exported():
    """_compute_missing flags absent metadata fields, not present ones."""
    from icx_engine.llm.base import _compute_missing
    from icx_engine.models.output import RawIssueData, IssueContext
    raw = RawIssueData(
        issue_key="P4-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="Medium", status="Open", metadata={},
        due_date="2026-06-15",
    )
    ctx = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=["step"], expected_behavior="e", actual_behavior="a",
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=1.0, missing_information=[],
    )
    missing = _compute_missing(ctx, raw)
    assert "due_date" not in missing


def test_attachments_module_importable():
    """Phase 4: attachments module is importable and exposes expected functions."""
    from icx_engine.connectors.attachments import _is_image, ocr_image, vision_enrich, process_attachments
    assert callable(_is_image)
    assert callable(ocr_image)
    assert callable(vision_enrich)
    assert callable(process_attachments)


def test_jira_client_has_download_attachment():
    """Phase 4: JiraClient exposes download_attachment method."""
    from icx_engine.connectors.jira.client import JiraClient
    assert hasattr(JiraClient, "download_attachment")
    assert callable(JiraClient.download_attachment)


def test_connect_jira_oauth_scopes_include_write():
    """OAuth scope bump: write:jira-work must be requested so a freshly-connected
    account can use jira_apply_update. Drives _connect_jira_oauth directly (debug=True
    re-raises past run_pkce_flow so the captured scopes are checked without needing a
    real browser/token exchange)."""
    import pytest
    from unittest.mock import patch
    from icx_engine.services import connection_service as cs

    captured = {}

    def fake_prompt(text, **kwargs):
        if "Client ID" in text:
            return "cid"
        if "Client Secret" in text:
            return "secret"
        if "base URL" in text:
            return "https://company.atlassian.net"
        return ""

    def fake_run_pkce_flow(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop-after-capture")

    with patch.object(cs.typer, "prompt", side_effect=fake_prompt), \
         patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch("icx_engine.auth.pkce.run_pkce_flow", fake_run_pkce_flow):
        with pytest.raises(RuntimeError):
            cs._connect_jira_oauth(debug=True)

    assert "write:jira-work" in captured["scopes"]


def test_model_add_routes_through_connection_service(cli_runner):
    """S1 regression guard: LLM credential prompts must route through
    connection_service, mirroring how the Jira/GitLab/Sonar connect flows
    already route their credential prompts - never prompt directly in cli.py."""
    from unittest.mock import patch
    from icx_engine.models.config import ChannelConfig

    text_cfg = ChannelConfig(provider="ollama", model="llama3")
    with patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch("icx_engine.services.connection_service._prompt_channel_config", return_value=text_cfg) as mock_text, \
         patch("icx_engine.services.connection_service._prompt_vision_channel", return_value=None) as mock_vision, \
         patch("icx_engine.cli._validate_and_save_model") as mock_validate:
        result = cli_runner.invoke(app, ["model", "--add"], input="work\n")

    assert result.exit_code == 0, result.output
    mock_text.assert_called_once()
    mock_vision.assert_called_once()
    mock_validate.assert_called_once()


def test_uae_optional_dependencies_importable():
    """Universal Attachment Engine converters (.xls, .pptx, scanned-PDF OCR) are importable."""
    import xlrd
    import pptx
    import fitz
    assert xlrd is not None
    assert pptx is not None
    assert fitz is not None


def test_model_no_flags_prints_help_hint(cli_runner):
    from unittest.mock import patch
    with patch.object(ConfigManager, "load", return_value=AppConfig()):
        result = cli_runner.invoke(app, ["model"])
    assert result.exit_code == 0
    assert "--add" in result.output or "model" in result.output


def test_connection_remove_invalid_target(cli_runner):
    from unittest.mock import patch
    with patch.object(ConfigManager, "load", return_value=AppConfig()):
        result = cli_runner.invoke(app, ["connection", "--remove", "xyz.atlassian.net"])
    assert result.exit_code != 0


def test_connection_active_invalid_target(cli_runner):
    from unittest.mock import patch
    with patch.object(ConfigManager, "load", return_value=AppConfig()):
        result = cli_runner.invoke(app, ["connection", "--active", "1"])
    assert result.exit_code != 0


def test_model_active_invalid_target(cli_runner):
    from unittest.mock import patch
    with patch.object(ConfigManager, "load", return_value=AppConfig()):
        result = cli_runner.invoke(app, ["model", "--active", "1"])
    assert result.exit_code != 0


def test_model_help_shows_remove_option(cli_runner):
    result = cli_runner.invoke(app, ["model", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    assert "--remove" in output


def test_model_remove_no_profiles(cli_runner):
    from unittest.mock import patch
    with patch.object(ConfigManager, "load", return_value=AppConfig()):
        result = cli_runner.invoke(app, ["model", "--remove", "work"])
    assert result.exit_code != 0


def test_model_remove_invalid_index(cli_runner):
    from unittest.mock import patch
    config = AppConfig(
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    with patch.object(ConfigManager, "load", return_value=config):
        with patch.object(ConfigManager, "save"):
            result = cli_runner.invoke(app, ["model", "--remove", "5"])
    assert result.exit_code != 0


def test_no_auto_download_on_graph_command(cli_runner):
    """icx graph <cmd> must never trigger a model download - only `icx setup` does."""
    with patch("icx_engine.memory.embeddings.EmbeddingsManager.ensure_ready") as mock_dl:
        cli_runner.invoke(app, ["graph", "--help"])
    mock_dl.assert_not_called()


def test_no_auto_download_on_memory_command(cli_runner):
    """icx memory list --help must not auto-download the model - only `icx setup` does."""
    with patch("icx_engine.memory.embeddings.EmbeddingsManager.ensure_ready") as mock_dl:
        cli_runner.invoke(app, ["memory", "list", "--help"])
    mock_dl.assert_not_called()


def test_memory_migrate_help(cli_runner):
    result = cli_runner.invoke(app, ["memory", "migrate", "--help"])
    assert result.exit_code == 0


def test_memory_by_file_help(cli_runner):
    result = cli_runner.invoke(app, ["memory", "by-file", "--help"])
    assert result.exit_code == 0


def test_memory_hotspots_help(cli_runner):
    result = cli_runner.invoke(app, ["memory", "hotspots", "--help"])
    assert result.exit_code == 0


def test_memory_related_help(cli_runner):
    result = cli_runner.invoke(app, ["memory", "related", "--help"])
    assert result.exit_code == 0


def test_memory_patterns_help(cli_runner):
    result = cli_runner.invoke(app, ["memory", "patterns", "--help"])
    assert result.exit_code == 0


def test_memory_update_help(cli_runner):
    result = cli_runner.invoke(app, ["memory", "update", "--help"])
    assert result.exit_code == 0


def test_memory_update_command_changes_field(cli_runner, tmp_path, monkeypatch):
    """icx memory update <KEY> prompts for a field and new value, then persists the change."""
    import uuid
    from unittest.mock import MagicMock, patch
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.schema import MemoryEntry

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [0.1] * 768
    mock_emb.check_ready.return_value = None
    mock_emb.ensure_ready.return_value = None

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(MemoryEntry(
            id=str(uuid.uuid4()),
            issue_key="PROJ-77",
            project_key="PROJ",
            source_type="jira",
            issue_type="Bug",
            summary="Old summary",
            problem_description="desc",
            resolution_note="note",
            files_changed=[],
            resolution_confirmed=False,
            saved_at="2026-01-01T00:00:00Z",
        ))

    monkeypatch.setattr("icx_engine.memory.manager.MemoryManager", lambda *a, **k: mgr)
    inputs = iter(["summary", "New summary"])
    monkeypatch.setattr("typer.prompt", lambda *a, **k: next(inputs))

    result = cli_runner.invoke(app, ["memory", "update", "PROJ-77"])
    assert result.exit_code == 0

    updated = mgr.show("PROJ-77")
    assert updated is not None
    assert updated.summary == "New summary"


def test_memory_export_import_roundtrip_preserves_confirmation_count(tmp_path):
    """Export then import must preserve confirmation_count exactly (no double-increment)."""
    import json
    import uuid
    from unittest.mock import patch, MagicMock
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.schema import MemoryEntry
    from icx_engine.memory.export import export_to_json, import_from_json

    mock_emb = MagicMock()
    mock_emb.embed.return_value = [0.1] * 768
    mock_emb.check_ready.return_value = None
    mock_emb.ensure_ready.return_value = None

    db_a = tmp_path / "db_a"
    db_b = tmp_path / "db_b"
    export_file = tmp_path / "export.json"

    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key="PROJ-99",
        project_key="PROJ",
        source_type="jira",
        issue_type="Bug",
        summary="Auth fails",
        problem_description="JWT expired",
        resolution_note="Updated TTL",
        files_changed=["src/auth/token.py"],
        resolution_confirmed=True,
        saved_at="2026-05-12T10:00:00Z",
        confirmation_count=4,
        memory_confidence=1.0,
    )

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        # Export from db_a
        mgr_a = MemoryManager(db_path=db_a)
        mgr_a.save(entry, restore=True)
        entries = mgr_a.list_entries()
        export_to_json(entries, export_file)

        # Import into db_b
        imported = import_from_json(export_file)
        mgr_b = MemoryManager(db_path=db_b)
        for e in imported:
            mgr_b.save(e, restore=True)

        result = mgr_b.show("PROJ-99")

    assert result is not None
    assert result.confirmation_count == 4
    assert result.memory_confidence == 1.0


def test_connection_add_duplicate_domain_prompts_overwrite(cli_runner):
    from unittest.mock import patch
    from icx_engine.connectors.jira.config import JiraConnection, TokenAuth

    existing = AppConfig(
        connections=[
            JiraConnection(
                domain="xyz.atlassian.net",
                auth=TokenAuth(auth_type="token", email="old@test.com", api_token="old-tok"),
            )
        ]
    )
    with patch.object(ConfigManager, "load", return_value=existing):
        # input: method=1 (API Token), domain=xyz.atlassian.net, then "n" to decline overwrite
        result = cli_runner.invoke(
            app, ["connection", "--add"],
            input="1\nhttps://xyz.atlassian.net\nn\n",
        )
    assert "already exists" in result.output
    assert "Cancelled" in result.output


def test_setup_help(cli_runner):
    result = cli_runner.invoke(app, ["setup", "--help"])
    assert result.exit_code == 0


def test_logout_help(cli_runner):
    result = cli_runner.invoke(app, ["logout", "--help"])
    assert result.exit_code == 0


def test_uninstall_help(cli_runner):
    result = cli_runner.invoke(app, ["uninstall", "--help"])
    assert result.exit_code in (0, 2)


def test_uninstall_log_lives_under_icx_home_not_shared_temp():
    from icx_engine.cli import _UNINSTALL_LOG
    # Security fix: a predictable filename in the shared OS temp dir is
    # spoofable by another local user - must live under the user's own
    # ~/.icx/ instead.
    import tempfile as _tempfile
    assert str(_tempfile.gettempdir()) not in str(_UNINSTALL_LOG.parent)
    assert _UNINSTALL_LOG.parent.name == ".icx"


def test_check_uninstall_result_reads_and_clears_failure_from_icx_home(monkeypatch, tmp_path):
    import icx_engine.cli as cli_mod
    log_path = tmp_path / ".icx" / "uninstall_result.txt"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("FAILED:exit 1", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "_UNINSTALL_LOG", log_path)

    cli_mod._check_uninstall_result()

    assert not log_path.exists()  # consumed exactly once


def test_check_uninstall_result_noop_when_log_absent(monkeypatch, tmp_path):
    import icx_engine.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_UNINSTALL_LOG", tmp_path / ".icx" / "uninstall_result.txt")
    cli_mod._check_uninstall_result()  # must not raise


def test_test_group_in_help(cli_runner):
    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "test" in result.output


def test_test_help_shows_subcommands(cli_runner):
    result = cli_runner.invoke(app, ["test", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    for cmd in ("sessions", "cancel"):
        assert cmd in output


def test_test_module_importable():
    from icx_engine.testing.state import TestingState, make_initial_state
    from icx_engine.testing.graph import get_db_path
    from icx_engine.testing.local_executor import run_local_verification
    assert callable(make_initial_state)
    assert callable(get_db_path)
    assert callable(run_local_verification)


def test_v2_testing_modules_importable():
    import icx_engine.testing.classify          # noqa: F401
    import icx_engine.testing.compat             # noqa: F401
    import icx_engine.testing.handlers           # noqa: F401
    import icx_engine.testing.expand             # noqa: F401
    from icx_engine.testing.nodes import (        # noqa: F401
        node_pick_type, node_compat_check, route_after_expand, route_after_compat,
    )


def test_appconfig_has_sonar_fields():
    from icx_engine.models.config import AppConfig
    cfg = AppConfig()
    assert hasattr(cfg, "sonar_project_key")
    assert hasattr(cfg, "sonar_token")
    assert "sonar_token" not in cfg.model_dump()


def test_appconfig_has_testing_fields():
    from icx_engine.models.config import AppConfig
    cfg = AppConfig()
    assert cfg.test_max_iterations == 3


def test_test_configure_sets_max_iterations(cli_runner, isolated_config):
    from icx_engine.cli import app
    from icx_engine.config_manager import ConfigManager
    # single prompt now: max fix iterations
    result = cli_runner.invoke(app, ["test", "configure"], input="7\n")
    assert result.exit_code == 0
    cfg = ConfigManager.load()
    assert cfg.test_max_iterations == 7


def test_test_configure_help_mentions_iterations(cli_runner):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["test", "configure", "--help"])
    assert result.exit_code == 0
    assert "iteration" in result.stdout.lower()


def test_test_setup_api_only_installs_pip_tools(cli_runner, monkeypatch):
    from icx_engine.cli import app
    import icx_engine.cli as _cli
    calls = []
    monkeypatch.setattr("icx_engine.testing.runners.install.is_installed", lambda name: False)
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None, node_dir=None: calls.append(name) or f"/opt/{name}")
    result = cli_runner.invoke(app, ["test", "setup", "-y", "--no-ui"])
    assert result.exit_code == 0
    assert calls == ["schemathesis", "hurl"]            # step 1 only, no Node


def test_test_setup_ui_prompts_node_and_saves_config(cli_runner, isolated_config, monkeypatch):
    from icx_engine.cli import app
    import icx_engine.cli as _cli
    from icx_engine.config_manager import ConfigManager
    calls = {}
    monkeypatch.setattr("icx_engine.testing.runners.install.is_installed", lambda name: False)

    def _ensure(name, approve=None, node_dir=None):
        calls["name"] = name
        calls["node_dir"] = node_dir
        return f"/opt/{name}"
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner", _ensure)
    # the interactive node resolver returns a validated executable
    monkeypatch.setattr(_cli, "_resolve_or_prompt_harness_node",
                        lambda yes, node_opt="": "/usr/local/node20/bin/node")

    result = cli_runner.invoke(app, ["test", "setup", "-y", "--no-api"])
    assert result.exit_code == 0
    assert calls["name"] == "playwright"
    assert calls["node_dir"].replace("\\", "/").endswith("node20/bin")   # node's dir on PATH for npm
    assert ConfigManager.load().harness_node_path == "/usr/local/node20/bin/node"


def test_resolve_reuses_valid_configured_node_without_prompt(monkeypatch):
    import icx_engine.cli as _cli
    from icx_engine import runtime_manager as rm
    monkeypatch.setattr(rm, "normalize_node_exe", lambda p: p or None)
    monkeypatch.setattr(rm, "validate_runtime", lambda lang, path: "20.11.0")   # >= 18
    monkeypatch.setattr("icx_engine.config_manager.ConfigManager.load",
                        staticmethod(lambda: type("C", (), {"harness_node_path": "/n20/node"})()))
    # yes=False but must NOT prompt - the configured node is already valid.
    assert _cli._resolve_or_prompt_harness_node(yes=False) == "/n20/node"


def test_resolve_node_opt_override(monkeypatch):
    import icx_engine.cli as _cli
    from icx_engine import runtime_manager as rm
    monkeypatch.setattr(rm, "normalize_node_exe", lambda p: p or None)
    monkeypatch.setattr(rm, "validate_runtime", lambda lang, path: "22.0.0")
    assert _cli._resolve_or_prompt_harness_node(False, node_opt="/x/node") == "/x/node"


def test_resolve_rejects_old_node_and_no_modern_found(monkeypatch):
    import icx_engine.cli as _cli
    from icx_engine import runtime_manager as rm
    monkeypatch.setattr(rm, "normalize_node_exe", lambda p: p or None)
    monkeypatch.setattr(rm, "validate_runtime", lambda lang, path: "16.20.0")   # < 18
    monkeypatch.setattr(rm, "discover_runtimes", lambda lang: [])               # nothing else
    monkeypatch.delenv("ICX_HARNESS_NODE", raising=False)
    monkeypatch.setattr("icx_engine.config_manager.ConfigManager.load",
                        staticmethod(lambda: type("C", (), {"harness_node_path": "/old/node"})()))
    assert _cli._resolve_or_prompt_harness_node(yes=True) is None


def test_test_setup_help_mentions_playwright(cli_runner):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["test", "setup", "--help"])
    assert result.exit_code == 0
    assert "playwright" in result.stdout.lower()


def test_appconfig_has_sonar_enable_fields():
    from icx_engine.models.config import AppConfig
    cfg = AppConfig()
    assert cfg.sonar_enabled is False
    assert cfg.sonar_url is None


def test_sonar_help_lists_commands(cli_runner):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["sonar", "--help"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    for cmd in ("add", "list", "active", "remove", "status", "projects", "report"):
        assert cmd in out


def test_sonar_report_no_connection_message(cli_runner, isolated_config):
    # default config has no Sonar connection -> report should guide, not crash
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["sonar", "report", "--project", "my-project"])
    assert "sonar add" in result.stdout.lower()


def test_sonar_report_prints_summary(cli_runner, isolated_config, monkeypatch):
    from icx_engine.cli import app
    from icx_engine.sonar import service
    captured = {}

    async def fake_report(project, branch=None, files=None, new_code_only=False):
        captured["project"] = project
        captured["files"] = files
        return {
            "quality_gate": {"status": "ERROR"},
            "measures": {"bugs": 3, "vulnerabilities": 1, "code_smells": 5,
                         "security_hotspots": 2, "coverage": 70.0,
                         "duplicated_lines_density": 4.0, "technical_debt": "1d"},
            "summary": {"total": 9, "by_severity": {"MAJOR": 9}},
            "truncated": False,
        }

    monkeypatch.setattr(service, "report", fake_report)
    result = cli_runner.invoke(
        app, ["sonar", "report", "--project", "my-project", "--file", "src/a.py"])
    assert result.exit_code == 0
    assert captured["project"] == "my-project"
    assert captured["files"] == ["src/a.py"]
    assert "error" in result.stdout.lower()


def test_version_tuple_handles_prerelease_suffix():
    """_version_tuple parses pre-release segments without raising (finding C2)."""
    from icx_engine.cli import _version_tuple
    assert _version_tuple("0.4.0rc1") == (0, 4, 0)
    assert _version_tuple("1.2.3") == (1, 2, 3)
    assert _version_tuple("0.4.0rc1") < _version_tuple("0.4.1")
    assert _version_tuple("2.0.0") > _version_tuple("1.9.9")
    assert _version_tuple("") == (0,)


def test_format_build_duration():
    """Graph-build duration shown in the 'Graph ready' summary."""
    from icx_engine.cli import _format_build_duration
    assert _format_build_duration(8) == "8s"
    assert _format_build_duration(45.4) == "45s"
    assert _format_build_duration(97) == "1m 37s"
    assert _format_build_duration(3660) == "1h 01m"


def test_clean_stale_artifacts_removes_profile_gen(tmp_path, monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_icx_dir", lambda: tmp_path)
    rules = tmp_path / "testing_rules"
    rules.mkdir(parents=True)
    stale = rules / "profile_gen.md"
    stale.write_text("old", encoding="utf-8")
    keep = rules / "2b.md"
    keep.write_text("live", encoding="utf-8")
    removed = cm.clean_stale_artifacts()
    assert str(stale) in removed
    assert not stale.exists()
    assert keep.exists()                       # live rulebooks untouched
    assert cm.clean_stale_artifacts() == []    # idempotent


def test_update_strips_stale_config_keys(cli_runner, isolated_config):
    from icx_engine.cli import app
    from icx_engine.config_manager import CONFIG_PATH, stale_config_keys_on_disk
    import json
    # simulate an older user's config carrying retired keys
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({
        "test_max_iterations": 3, "agent_max_steps": 100, "magik_base_url": "http://x",
    }), encoding="utf-8")
    assert set(stale_config_keys_on_disk()) == {"agent_max_steps", "magik_base_url"}
    result = cli_runner.invoke(app, ["update"])
    assert result.exit_code == 0
    on_disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert "agent_max_steps" not in on_disk       # stripped from disk
    assert "magik_base_url" not in on_disk
    assert "test_max_iterations" in on_disk        # live field kept
    assert stale_config_keys_on_disk() == []       # nothing stale remains


def test_update_seeds_default_skills(cli_runner, isolated_config, monkeypatch):
    from icx_engine.cli import app
    calls = []

    def _fake_seed():
        calls.append(True)
        return {"seeded": ["systematic-debugging"], "updated": [], "skipped_customized": []}

    monkeypatch.setattr("icx_engine.skills.seed.seed_default_skills", _fake_seed)
    result = cli_runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert calls == [True]
    assert "Default skills" in result.stdout


import respx
import httpx


@respx.mock
def test_gitlab_connect_command_saves_connection(cli_runner, isolated_config, monkeypatch):
    from icx_engine.cli import app
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "u", "name": "U"})
    )
    monkeypatch.setattr("typer.prompt", lambda *a, **k: {
        "Connection name": "default",
        "GitLab server URL": "https://gitlab.example.com",
        "Personal access token (blank to keep existing)": "glpat-x",
    }.get(a[0], ""))
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    result = cli_runner.invoke(app, ["gitlab", "--add"])
    assert result.exit_code == 0
    from icx_engine.config_manager import ConfigManager
    config = ConfigManager.load()
    assert config.active_gitlab == "default"
    assert config.gitlab_connections["default"].token == "glpat-x"


def test_gitlab_status_command_reports_not_configured(cli_runner, isolated_config):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["gitlab", "status"])
    assert result.exit_code == 0
    assert "not configured" in result.stdout.lower() or "none" in result.stdout.lower()


def test_gitlab_remove_command_with_unknown_name_reports_cleanly(cli_runner, isolated_config):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["gitlab", "--remove", "somename"])
    assert result.exit_code != 0


def test_gitlab_bare_invocation_lists_connections(cli_runner, isolated_config):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["gitlab"])
    assert result.exit_code == 0
    assert "no gitlab connections" in result.stdout.lower()


@respx.mock
def test_gitlab_active_and_list_flags(cli_runner, isolated_config, monkeypatch):
    from icx_engine.cli import app
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "u1", "name": "U1"})
    )
    respx.get("https://gitlab.staging.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 2, "username": "u2", "name": "U2"})
    )
    prompts = iter([
        {"Connection name": "prod", "GitLab server URL": "https://gitlab.example.com", "Personal access token (blank to keep existing)": "t1"},
        {"Connection name": "staging", "GitLab server URL": "https://gitlab.staging.com", "Personal access token (blank to keep existing)": "t2"},
    ])
    current = {"d": {}}

    def _prompt(*a, **k):
        return current["d"].get(a[0], "")

    monkeypatch.setattr("typer.prompt", _prompt)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)

    current["d"] = next(prompts)
    result = cli_runner.invoke(app, ["gitlab", "--add"])
    assert result.exit_code == 0

    current["d"] = next(prompts)
    result = cli_runner.invoke(app, ["gitlab", "--add"])
    assert result.exit_code == 0

    result = cli_runner.invoke(app, ["gitlab", "--list"])
    assert result.exit_code == 0
    assert "prod" in result.stdout and "staging" in result.stdout

    result = cli_runner.invoke(app, ["gitlab", "--active", "prod"])
    assert result.exit_code == 0
    from icx_engine.config_manager import ConfigManager
    assert ConfigManager.load().active_gitlab == "prod"

    result = cli_runner.invoke(app, ["gitlab", "--remove", "staging"])
    assert result.exit_code == 0
    assert "staging" not in ConfigManager.load().gitlab_connections


@respx.mock
def test_gitlab_connect_with_debug_still_hides_token_input(cli_runner, isolated_config, monkeypatch):
    """Verify that --debug flag does NOT bypass token input hiding.

    This is a security test: the token prompt should ALWAYS use hide_input=True,
    even when --debug is passed. Plaintext token echo is a critical security issue.
    """
    from icx_engine.cli import app
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "u", "name": "U"})
    )

    # Track kwargs passed to typer.prompt to verify hide_input setting
    call_kwargs = {}

    def mock_prompt(*a, **k):
        # Capture kwargs for the token field prompt
        if "token" in str(a[0]).lower():
            call_kwargs["token_prompt_kwargs"] = k.copy()
        # Return default values matching existing test pattern
        prompts = {
            "Connection name": "default",
            "GitLab server URL": "https://gitlab.example.com",
            "Personal access token (blank to keep existing)": "glpat-x",
        }
        return prompts.get(a[0], "")

    monkeypatch.setattr("typer.prompt", mock_prompt)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)

    # Invoke with --debug flag
    result = cli_runner.invoke(app, ["gitlab", "--add", "--debug"])
    assert result.exit_code == 0

    # Verify the token prompt was called
    assert "token_prompt_kwargs" in call_kwargs, "Token prompt was not called"

    # CRITICAL: Even with --debug, hide_input must be True
    assert call_kwargs["token_prompt_kwargs"]["hide_input"] is True, \
        "SECURITY BUG: Token prompt called with hide_input=False when --debug was passed"
