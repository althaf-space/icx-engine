import pytest

from icx_engine.exceptions import InvalidInput
from icx_engine.sonar.parse import parse_sonar_url


def test_dashboard_url_extracts_base_project_branch():
    out = parse_sonar_url("http://host:9000/dashboard?id=my-project&branch=feature/x")
    assert out.base_url == "http://host:9000"
    assert out.project_key == "my-project"
    assert out.branch == "feature/x"


def test_bare_base_url():
    out = parse_sonar_url("https://sonar.example.com")
    assert out.base_url == "https://sonar.example.com"
    assert out.project_key is None
    assert out.branch is None


def test_trailing_path_stripped_to_base():
    out = parse_sonar_url("http://10.0.0.5:9000/dashboard")
    assert out.base_url == "http://10.0.0.5:9000"
    assert out.project_key is None


def test_id_without_branch():
    out = parse_sonar_url("http://host:9000/dashboard?id=service-api")
    assert out.project_key == "service-api"
    assert out.branch is None


def test_non_http_rejected():
    with pytest.raises(InvalidInput):
        parse_sonar_url("ftp://host/x")


def test_embedded_credentials_rejected():
    with pytest.raises(InvalidInput):
        parse_sonar_url("http://user:pass@host:9000")


def test_control_characters_rejected():
    with pytest.raises(InvalidInput):
        parse_sonar_url("http://host:9000\n/dashboard")
