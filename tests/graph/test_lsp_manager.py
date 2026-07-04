"""LSP binary integrity: pinned versions + checksum/strict-mode (finding S1b)."""
import hashlib
import io
import urllib.request

import pytest

from icx_engine.graph.parser import lsp_manager as m


def _fake_urlopen(data: bytes):
    def _fn(req, timeout=None):
        return io.BytesIO(data)  # BytesIO supports read() + context manager
    return _fn


def test_lsp_urls_are_pinned_not_latest():
    assert "latest" not in m._KOTLIN_LS_URL
    assert "latest" not in m._RUST_ANALYZER_BASE
    assert "latest" not in m._OMNISHARP_BASE
    assert m._CLANGD_VERSION and "latest" not in m._CLANGD_VERSION
    # Pinned versions appear in their URLs.
    assert m._KOTLIN_LS_VERSION in m._KOTLIN_LS_URL
    assert m._RUST_ANALYZER_VERSION in m._RUST_ANALYZER_BASE
    assert m._OMNISHARP_VERSION in m._OMNISHARP_BASE


def test_download_lsp_checksum_match(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_LSP_CHECKSUMS", {"srv": hashlib.sha256(b"data").hexdigest()})
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"data"))
    dest = tmp_path / "srv.bin"
    m._download_lsp("https://example.com/srv", dest, "srv")  # no raise
    assert dest.read_bytes() == b"data"


def test_download_lsp_checksum_mismatch_raises_and_unlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_LSP_CHECKSUMS", {"srv": "0" * 64})
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"data"))
    dest = tmp_path / "srv.bin"
    with pytest.raises(ValueError, match="Checksum mismatch"):
        m._download_lsp("https://example.com/srv", dest, "srv")
    assert not dest.exists()


def test_download_lsp_strict_env_refuses_unpinned(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_LSP_CHECKSUMS", {})
    monkeypatch.setenv("ICX_REQUIRE_LSP_CHECKSUM", "1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"data"))
    dest = tmp_path / "srv.bin"
    with pytest.raises(ValueError, match="ICX_REQUIRE_LSP_CHECKSUM"):
        m._download_lsp("https://example.com/srv", dest, "srv")
    assert not dest.exists()


def test_download_lsp_unset_env_allows_unpinned(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_LSP_CHECKSUMS", {})
    monkeypatch.delenv("ICX_REQUIRE_LSP_CHECKSUM", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(b"data"))
    dest = tmp_path / "srv.bin"
    m._download_lsp("https://example.com/srv", dest, "srv")  # no raise (prior behavior)
    assert dest.read_bytes() == b"data"
