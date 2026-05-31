def test_role_tag_controller():
    from icx_engine.graph.parser.roles import role_tag
    assert role_tag("app/controllers/user_controller.py") == "[controller]"

def test_role_tag_service():
    from icx_engine.graph.parser.roles import role_tag
    assert role_tag("services/user_service.go") == "[service]"

def test_role_tag_none():
    from icx_engine.graph.parser.roles import role_tag
    assert role_tag("main.go") == ""

def test_role_tag_hook():
    from icx_engine.graph.parser.roles import role_tag
    assert role_tag("src/hooks/useCurrentUser.ts") == "[hook]"
