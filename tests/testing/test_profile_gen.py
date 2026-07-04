from icx_engine.testing.profile_gen import build_profile_markdown


def test_profile_has_required_sections():
    classified = [
        {"path": "src/pages/CreateUser.tsx", "layer": "frontend", "role": "container",
         "artifacts": ["component", "route"], "testability": {}},
    ]
    md = build_profile_markdown(classified, "Proj A", "http://host-x")
    assert "## Description" in md
    assert "## Base URL" in md
    assert "http://host-x" in md
    assert "### Screen:" in md
    assert "CreateUser" in md


def test_create_screen_gets_create_functionality():
    classified = [
        {"path": "src/pages/CreateUser.tsx", "layer": "frontend", "role": "container",
         "artifacts": ["component"], "testability": {}},
    ]
    md = build_profile_markdown(classified, "Proj A", "http://host-x")
    assert "functionality: create" in md.lower()


def test_backend_files_excluded_from_screens():
    classified = [
        {"path": "src/UserController.java", "layer": "backend", "role": "controller",
         "artifacts": ["endpoint"], "testability": {}},
    ]
    md = build_profile_markdown(classified, "Proj A", "http://host-x")
    assert "### Screen:" not in md
