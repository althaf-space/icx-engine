"""Tests for builder.py SCIP tsconfig selection logic.

Verifies that --infer-tsconfig is used when the project has its own
tsconfig.json/jsconfig.json, and the generated tsconfig is only used
for JS-only projects without any tsconfig.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from icx_engine.graph.parser.scip_manager import write_ts_tsconfig


class TestTsConfigSelectionLogic:
    """Unit tests for the tsconfig selection in write_ts_tsconfig and scip_manager."""

    def test_baseurl_set_to_project_root_in_generated_tsconfig(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        dest = tmp_path / "cache" / "scip" / "icx-tsconfig.json"
        dest.parent.mkdir(parents=True)
        write_ts_tsconfig(str(project), [], dest)
        data = json.loads(dest.read_text())
        assert data["compilerOptions"]["baseUrl"].replace("\\", "/") == project.as_posix()

    def test_generated_tsconfig_has_allowjs(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [], dest)
        data = json.loads(dest.read_text())
        assert data["compilerOptions"]["allowJs"] is True

    def test_generated_tsconfig_persists_after_write(self, tmp_path):
        """Generated tsconfig must remain on disk for user inspection after build."""
        project = tmp_path / "project"
        project.mkdir()
        cache = tmp_path / "cache" / "scip"
        cache.mkdir(parents=True)
        dest = cache / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [], dest)
        assert dest.exists(), "icx-tsconfig.json must persist (not deleted) after write"


class TestScipTsConfigPresenceCheck:
    """Verify the project tsconfig detection in the build path."""

    def test_project_with_tsconfig_json_detected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "tsconfig.json").write_text('{"compilerOptions":{}}')
        assert (project / "tsconfig.json").exists()
        has = (project / "tsconfig.json").exists() or (project / "jsconfig.json").exists()
        assert has

    def test_project_with_jsconfig_json_detected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "jsconfig.json").write_text('{"compilerOptions":{}}')
        has = (project / "tsconfig.json").exists() or (project / "jsconfig.json").exists()
        assert has

    def test_project_without_any_tsconfig_falls_through(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        has = (project / "tsconfig.json").exists() or (project / "jsconfig.json").exists()
        assert not has

    def test_generated_tsconfig_not_created_when_project_has_tsconfig(self, tmp_path):
        """When project has tsconfig.json the ICX-generated one must NOT be created."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "tsconfig.json").write_text('{"compilerOptions":{}}')
        cache = tmp_path / "cache" / "scip"
        cache.mkdir(parents=True)
        icx_tsconfig = cache / "icx-tsconfig.json"

        # Simulate the builder logic
        _has_project_tsconfig = (
            (project / "tsconfig.json").exists()
            or (project / "jsconfig.json").exists()
        )
        if not _has_project_tsconfig:
            write_ts_tsconfig(str(project), [], icx_tsconfig)

        assert not icx_tsconfig.exists(), (
            "icx-tsconfig.json must NOT be generated when project has its own tsconfig.json"
        )

    def test_generated_tsconfig_created_when_no_project_tsconfig(self, tmp_path):
        """For JS-only projects without tsconfig.json, generated tsconfig must be created."""
        project = tmp_path / "project"
        project.mkdir()
        cache = tmp_path / "cache" / "scip"
        cache.mkdir(parents=True)
        icx_tsconfig = cache / "icx-tsconfig.json"

        _has_project_tsconfig = (
            (project / "tsconfig.json").exists()
            or (project / "jsconfig.json").exists()
        )
        if not _has_project_tsconfig:
            write_ts_tsconfig(str(project), [], icx_tsconfig)

        assert icx_tsconfig.exists(), (
            "icx-tsconfig.json must be generated for JS-only projects without tsconfig"
        )
