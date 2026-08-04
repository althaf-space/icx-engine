from __future__ import annotations
import pytest


@pytest.fixture
def workstatus_base_url() -> str:
    return "https://web-api.workstatus.io"


@pytest.fixture
def workstatus_creds() -> dict:
    return {
        "user_id": "175599",
        "org_id": "8570",
        "authorization": "Bearer test-auth-token",
        "sd_token": "test-sd-token",
        "device_type": "web",
    }
