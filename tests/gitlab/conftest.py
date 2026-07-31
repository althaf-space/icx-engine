from __future__ import annotations
import pytest
import respx


@pytest.fixture
def gitlab_base_url() -> str:
    return "https://gitlab.example.com"
