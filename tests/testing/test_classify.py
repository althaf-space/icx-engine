from icx_engine.testing.classify import classify_file, FileClass


def test_jsx_component_with_testid_is_frontend_testable():
    content = (
        'export default function CreateUser() {\n'
        '  return <form><button data-testid="submit">Save</button></form>;\n'
        '}\n'
    )
    fc = classify_file("src/pages/CreateUser.tsx", content)
    assert isinstance(fc, FileClass)
    assert fc.layer == "frontend"
    assert "component" in fc.artifacts
    assert fc.testability["has_stable_selector"] is True
    assert fc.testability["renderable"] is True


def test_spring_controller_is_backend_endpoint():
    content = (
        '@RestController\n'
        '@RequestMapping("/api/users")\n'
        'public class UserController {\n'
        '  @PostMapping\n'
        '  public User create(@RequestBody UserDto dto) { return svc.create(dto); }\n'
        '}\n'
    )
    fc = classify_file("src/main/java/app/UserController.java", content)
    assert fc.layer == "backend"
    assert "endpoint" in fc.artifacts
    assert fc.testability["exposes_endpoint"] is True
    assert fc.testability["has_request_schema"] is True


def test_frontend_component_missing_selector():
    content = "export default function Plain() { return <div>hi</div>; }\n"
    fc = classify_file("src/components/Plain.jsx", content)
    assert fc.layer == "frontend"
    assert fc.testability["has_stable_selector"] is False


def test_unknown_when_no_content_and_neutral_extension():
    fc = classify_file("README.md", None)
    assert fc.layer in ("shared", "unknown")


def test_python_fastapi_route_backend():
    content = (
        'from fastapi import APIRouter\n'
        'router = APIRouter()\n'
        '@router.post("/items")\n'
        'def create_item(item: Item):\n'
        '    return item\n'
    )
    fc = classify_file("app/routes/items.py", content)
    assert fc.layer == "backend"
    assert fc.testability["exposes_endpoint"] is True
