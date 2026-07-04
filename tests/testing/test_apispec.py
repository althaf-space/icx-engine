from icx_engine.testing.apispec import extract_endpoint, build_request_spec, Endpoint


def test_extract_spring_post():
    content = (
        '@RestController\n@RequestMapping("/api/users")\n'
        'class UserController {\n'
        '  @PostMapping\n  public User create(@RequestBody UserDto dto) { }\n}\n'
    )
    ep = extract_endpoint("UserController.java", content)
    assert ep is not None
    assert ep.method == "POST"
    assert "/api/users" in ep.path


def test_extract_fastapi_post():
    content = '@router.post("/items")\ndef create_item(item: Item):\n    return item\n'
    ep = extract_endpoint("items.py", content)
    assert ep.method == "POST"
    assert ep.path == "/items"


def test_extract_returns_none_when_no_endpoint():
    assert extract_endpoint("util.py", "def add(a, b): return a + b\n") is None


def test_build_request_spec_shape():
    ep = Endpoint(method="POST", path="/api/users", sample_body={"name": "x"})
    spec = build_request_spec("http://host-x", ep, headers={"Authorization": "Bearer t"})
    assert spec["url"] == "http://host-x/api/users"
    assert spec["method"] == "POST"
    assert spec["headers"]["Authorization"] == "Bearer t"
    assert spec["body"] == {"name": "x"}


def test_build_request_spec_joins_path_without_double_slash():
    ep = Endpoint(method="GET", path="/items", sample_body=None)
    spec = build_request_spec("http://host-x/", ep)
    assert spec["url"] == "http://host-x/items"
