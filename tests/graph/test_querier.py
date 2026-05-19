"""Tests for graph/querier.py - generate_graph_report, _role_tag, _sanitize_cluster_filename."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from icx_engine.graph.querier import generate_graph_report, _role_tag, _sanitize_cluster_filename


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_graph(tmp_path: Path, data: dict, name: str = "graph.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _report(tmp_path: Path) -> Path:
    return tmp_path / "GRAPH_REPORT.md"


# ---------------------------------------------------------------------------
# Error / edge cases
# ---------------------------------------------------------------------------

def test_corrupted_graph_writes_unavailable_report(tmp_path):
    bad = tmp_path / "graph.json"
    bad.write_text("not valid json", encoding="utf-8")
    out = _report(tmp_path)
    generate_graph_report(bad, out)
    assert out.exists()
    assert "unavailable" in out.read_text(encoding="utf-8").lower()


def test_missing_graph_file_writes_unavailable_report(tmp_path):
    out = _report(tmp_path)
    generate_graph_report(tmp_path / "nonexistent.json", out)
    assert out.exists()
    assert "unavailable" in out.read_text(encoding="utf-8").lower()


def test_empty_nodes_writes_no_nodes_report(tmp_path):
    gf = _write_graph(tmp_path, {"nodes": [], "edges": []})
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "No nodes" in content


# ---------------------------------------------------------------------------
# Basic report structure
# ---------------------------------------------------------------------------

SIMPLE_GRAPH = {
    "nodes": [
        {"id": "cli", "source_file": "src/cli.py"},
        {"id": "engine", "source_file": "src/engine.py"},
        {"id": "auth", "source_file": "src/auth.py"},
    ],
    "edges": [
        {"source": "cli", "target": "engine"},
        {"source": "engine", "target": "auth"},
    ],
}


def test_report_is_created(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert out.exists()


def test_report_has_graph_report_heading(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert "# Project Graph Report" in out.read_text(encoding="utf-8")


def test_report_has_community_clusters_section(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert "Community Clusters" in out.read_text(encoding="utf-8")


def test_report_has_god_nodes_section(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert "God Nodes" in out.read_text(encoding="utf-8")


def test_report_lists_source_files(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "src/cli.py" in content or "src/engine.py" in content or "src/auth.py" in content


# ---------------------------------------------------------------------------
# Community assignment - priority 1: top-level communities key
# ---------------------------------------------------------------------------

def test_communities_key_priority(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a.py"},
            {"id": "b", "source_file": "src/b.py"},
            {"id": "c", "source_file": "src/c.py"},
            {"id": "d", "source_file": "src/d.py"},
        ],
        "edges": [],
        "communities": {"group1": ["a", "b"], "group2": ["c", "d"]},
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    # Two distinct communities -> two cluster files
    assert clusters_dir.exists()
    assert len(list(clusters_dir.glob("*.md"))) >= 2


# ---------------------------------------------------------------------------
# Community assignment - priority 2: node-level community attribute
# ---------------------------------------------------------------------------

def test_node_level_community_attribute(tmp_path):
    graph = {
        "nodes": [
            {"id": "x", "source_file": "src/x.py", "community": 0},
            {"id": "y", "source_file": "src/y.py", "community": 0},
            {"id": "z", "source_file": "src/z.py", "community": 1},
            {"id": "w", "source_file": "src/w.py", "community": 1},
        ],
        "edges": [],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    assert clusters_dir.exists()
    assert len(list(clusters_dir.glob("*.md"))) >= 2


# ---------------------------------------------------------------------------
# Community assignment - priority 3: directory fallback
# ---------------------------------------------------------------------------

def test_directory_based_community_fallback(tmp_path):
    graph = {
        "nodes": [
            {"id": "s1", "source_file": "src/services/UserService.py"},
            {"id": "s2", "source_file": "src/services/AuthService.py"},
            {"id": "m1", "source_file": "src/models/User.py"},
            {"id": "m2", "source_file": "src/models/Order.py"},
        ],
        "edges": [],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    # services and models are different dirs -> two cluster files
    assert clusters_dir.exists()
    assert len(list(clusters_dir.glob("*.md"))) >= 2


# ---------------------------------------------------------------------------
# God nodes detection
# ---------------------------------------------------------------------------

def test_god_nodes_detected_for_hub_file(tmp_path):
    # hub.py connects to many nodes - should be identified as god node
    nodes = [{"id": "hub", "source_file": "src/hub.py"}]
    nodes += [{"id": f"leaf{i}", "source_file": f"src/leaf{i}.py"} for i in range(10)]
    edges = [{"source": "hub", "target": f"leaf{i}"} for i in range(10)]
    gf = _write_graph(tmp_path, {"nodes": nodes, "edges": edges})
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "src/hub.py" in content


def test_god_nodes_none_when_uniform_degree(tmp_path):
    # All files have equal connectivity - no outliers
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a.py"},
            {"id": "b", "source_file": "src/b.py"},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "none identified" in content.lower() or "God Nodes" in content


# ---------------------------------------------------------------------------
# Cross-cluster connections
# ---------------------------------------------------------------------------

def test_cross_cluster_connections_reported(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/services/A.py", "community": 0},
            {"id": "b", "source_file": "src/models/B.py", "community": 1},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "Cross-Cluster" in content


def test_no_cross_cluster_section_for_single_community(tmp_path):
    # All nodes in same community - cross-cluster section should not appear
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/services/A.py", "community": 0},
            {"id": "b", "source_file": "src/services/B.py", "community": 0},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "Cross-Cluster" not in content


# ---------------------------------------------------------------------------
# Degree-based ordering
# ---------------------------------------------------------------------------

def test_high_degree_files_listed_first_in_cluster(tmp_path):
    # engine.py has 2 edges, cli.py has 1 - engine should appear first in cluster file
    graph = {
        "nodes": [
            {"id": "cli", "source_file": "src/cli.py"},
            {"id": "engine", "source_file": "src/engine.py"},
            {"id": "util", "source_file": "src/util.py"},
        ],
        "edges": [
            {"source": "cli", "target": "engine"},
            {"source": "util", "target": "engine"},
        ],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    cluster_files = list(clusters_dir.glob("*.md"))
    assert len(cluster_files) >= 1
    content = cluster_files[0].read_text(encoding="utf-8")
    engine_pos = content.find("src/engine.py")
    cli_pos = content.find("src/cli.py")
    assert engine_pos != -1
    assert cli_pos != -1
    assert engine_pos < cli_pos


# ---------------------------------------------------------------------------
# Nodes without source_file are skipped gracefully
# ---------------------------------------------------------------------------

def test_nodes_without_source_file_skipped(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a.py"},
            {"id": "a2", "source_file": "src/a2.py"},
            {"id": "b"},  # no source_file
        ],
        "edges": [],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "src/a.py" in content


# ---------------------------------------------------------------------------
# Footer / metadata
# ---------------------------------------------------------------------------

def test_report_has_icx_footer(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "ICX" in content or "icx" in content.lower()


# ---------------------------------------------------------------------------
# New: cluster files, role tags, descriptions, deduplication
# ---------------------------------------------------------------------------

def test_cluster_files_created_in_graph_clusters_dir(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    assert clusters_dir.exists()
    assert len(list(clusters_dir.glob("*.md"))) >= 1


def test_cluster_file_contains_source_files(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    all_content = "\n".join(f.read_text(encoding="utf-8") for f in clusters_dir.glob("*.md"))
    assert "src/cli.py" in all_content or "src/engine.py" in all_content


def test_cluster_file_contains_role_tags_for_java(tmp_path):
    graph = {
        "nodes": [
            {"id": "ctrl", "source_file": "src/main/UserController.java"},
            {"id": "svc", "source_file": "src/main/UserService.java"},
        ],
        "edges": [{"source": "ctrl", "target": "svc"}],
        "communities": {"0": ["ctrl", "svc"]},
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    all_content = "\n".join(f.read_text(encoding="utf-8") for f in clusters_dir.glob("*.md"))
    assert "[controller]" in all_content
    assert "[service]" in all_content


def test_index_has_cluster_table(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "|" in content


def test_cluster_descriptions_rendered_when_available(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a.py"},
            {"id": "b", "source_file": "src/b.py"},
        ],
        "edges": [],
        "communities": {"42": ["a", "b"]},
    }
    gf = _write_graph(tmp_path, graph)
    desc_path = tmp_path / "cluster_descriptions.json"
    desc_path.write_text('{"42": "Core authentication logic."}', encoding="utf-8")
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    index_content = out.read_text(encoding="utf-8")
    assert "Core authentication logic." in index_content
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    cluster_content = "\n".join(f.read_text(encoding="utf-8") for f in clusters_dir.glob("*.md"))
    assert "Core authentication logic." in cluster_content


def test_duplicate_cluster_labels_get_unique_filenames(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a/Modal.jsx"},
            {"id": "b", "source_file": "src/a/EditModal.jsx"},
            {"id": "c", "source_file": "src/b/SaveModal.jsx"},
            {"id": "d", "source_file": "src/b/ConfirmModal.jsx"},
        ],
        "edges": [],
        "communities": {"0": ["a", "b"], "1": ["c", "d"]},
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    filenames = [f.name for f in clusters_dir.glob("*.md")]
    assert len(filenames) == len(set(filenames))


def test_case_insensitive_dedup_produces_unique_files(tmp_path):
    # "Modal" and "modal" would collide on Windows (case-insensitive filesystem)
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/Modal.jsx"},
            {"id": "b", "source_file": "src/ModalHelper.jsx"},
            {"id": "c", "source_file": "src/modal.jsx"},
            {"id": "d", "source_file": "src/modalUtils.jsx"},
        ],
        "edges": [],
        "communities": {"0": ["a", "b"], "1": ["c", "d"]},
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    files = list(clusters_dir.glob("*.md"))
    # Both communities must produce files - no silent overwrite
    assert len(files) == 2
    # Filenames must differ case-insensitively
    lower_names = [f.name.lower() for f in files]
    assert len(lower_names) == len(set(lower_names))


def test_stale_cluster_files_removed_on_rebuild(tmp_path):
    # First build: 2 communities -> 2 cluster files
    graph_v1 = {
        "nodes": [
            {"id": "a", "source_file": "src/services/UserService.py"},
            {"id": "b", "source_file": "src/services/AuthService.py"},
            {"id": "c", "source_file": "src/models/User.py"},
            {"id": "d", "source_file": "src/models/Order.py"},
        ],
        "edges": [],
        "communities": {"0": ["a", "b"], "1": ["c", "d"]},
    }
    gf = _write_graph(tmp_path, graph_v1)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    clusters_dir = tmp_path / "GRAPH_CLUSTERS"
    assert len(list(clusters_dir.glob("*.md"))) == 2

    # Second build: only 1 community (services cluster dropped)
    graph_v2 = {
        "nodes": [
            {"id": "c", "source_file": "src/models/User.py"},
            {"id": "d", "source_file": "src/models/Order.py"},
        ],
        "edges": [],
        "communities": {"1": ["c", "d"]},
    }
    gf2 = _write_graph(tmp_path, graph_v2, "graph2.json")
    generate_graph_report(gf2, out)
    remaining = list(clusters_dir.glob("*.md"))
    # Stale file from first build must be removed - only 1 cluster now
    assert len(remaining) == 1


# ---------------------------------------------------------------------------
# New: _role_tag - Java
# ---------------------------------------------------------------------------

def test_role_tag_java_controller():
    assert _role_tag("src/main/java/com/app/UserRestController.java") == "[controller]"

def test_role_tag_java_service_impl():
    assert _role_tag("src/main/java/com/app/UserServiceImpl.java") == "[service]"

def test_role_tag_java_service():
    assert _role_tag("src/main/java/com/app/UserService.java") == "[service]"

def test_role_tag_java_dao_impl():
    assert _role_tag("src/main/java/com/app/UserDAOImpl.java") == "[dao]"

def test_role_tag_java_dao():
    assert _role_tag("src/main/java/com/app/UserDao.java") == "[dao]"

def test_role_tag_java_dto():
    assert _role_tag("src/main/java/com/app/UserDTO.java") == "[model]"

def test_role_tag_java_to():
    assert _role_tag("src/main/java/com/app/ScheduleTO.java") == "[model]"

def test_role_tag_java_bean():
    assert _role_tag("src/main/java/com/app/OfferRequestBean.java") == "[model]"

def test_role_tag_java_config():
    assert _role_tag("src/main/java/com/app/CorsFilterConfig.java") == "[config]"

def test_role_tag_java_util():
    assert _role_tag("src/main/java/com/app/LoggerUtil.java") == "[util]"

def test_role_tag_java_test():
    assert _role_tag("src/test/java/com/app/UserServiceTest.java") == "[test]"

def test_role_tag_java_unknown_returns_empty():
    assert _role_tag("src/main/java/com/app/SomeWrapper.java") == ""


# ---------------------------------------------------------------------------
# New: _role_tag - JS/JSX
# ---------------------------------------------------------------------------

def test_role_tag_js_hook():
    assert _role_tag("src/hooks/useAuth.js") == "[hook]"

def test_role_tag_js_hook_use_prefix():
    assert _role_tag("src/useTheme.ts") == "[hook]"

def test_role_tag_jsx_modal():
    assert _role_tag("src/containers/user/DeleteModal.jsx") == "[modal]"

def test_role_tag_js_action():
    assert _role_tag("src/redux/actions/userActions.js") == "[action]"

def test_role_tag_js_reducer():
    assert _role_tag("src/redux/reducers/userReducer.js") == "[reducer]"

def test_role_tag_jsx_container():
    assert _role_tag("src/containers/UserList.jsx") == "[container]"

def test_role_tag_jsx_component():
    assert _role_tag("src/components/Button.jsx") == "[component]"

def test_role_tag_js_util_stem():
    assert _role_tag("src/Util.js") == "[util]"

def test_role_tag_js_route():
    assert _role_tag("src/appconfig/routes/DashboardRoutes.jsx") == "[route]"

def test_role_tag_js_routeconfig():
    assert _role_tag("src/RouteConfig/Pages.jsx") == "[route]"

def test_role_tag_js_appconfig():
    assert _role_tag("src/appconfig/api.jsx") == "[config]"

def test_role_tag_non_code_file_returns_empty():
    assert _role_tag("README.md") == ""

# -----------------------------------------------------------------------
# _role_tag - universal cross-language patterns
# -----------------------------------------------------------------------

def test_role_tag_python_django_views_plural():
    assert _role_tag("myapp/views.py") == "[controller]"

def test_role_tag_phoenix_view_not_controller():
    # Phoenix views are presenters - must NOT be [controller]; /views/ dir -> [container]
    assert _role_tag("lib/myapp_web/views/user_view.ex") == "[container]"

def test_role_tag_python_django_models():
    assert _role_tag("myapp/models.py") == "[model]"

def test_role_tag_python_django_serializers():
    assert _role_tag("myapp/serializers.py") == "[model]"

def test_role_tag_python_django_urls():
    assert _role_tag("myapp/urls.py") == "[route]"

def test_role_tag_python_settings():
    assert _role_tag("config/settings.py") == "[config]"

def test_role_tag_python_celery_tasks():
    assert _role_tag("myapp/task.py") == "[service]"

def test_role_tag_go_handler():
    assert _role_tag("internal/api/user_handler.go") == "[controller]"

def test_role_tag_go_service():
    assert _role_tag("internal/service/user_service.go") == "[service]"

def test_role_tag_go_repository():
    assert _role_tag("internal/repo/user_repo.go") == "[dao]"

def test_role_tag_go_test():
    assert _role_tag("internal/service/user_service_test.go") == "[test]"

def test_role_tag_csharp_controller():
    assert _role_tag("Controllers/UserController.cs") == "[controller]"

def test_role_tag_csharp_repository():
    assert _role_tag("Data/UserRepository.cs") == "[dao]"

def test_role_tag_ruby_controller():
    assert _role_tag("app/controllers/users_controller.rb") == "[controller]"

def test_role_tag_ruby_model():
    assert _role_tag("app/models/user.rb") == "[model]"

def test_role_tag_php_controller():
    assert _role_tag("app/Http/Controllers/UserController.php") == "[controller]"

def test_role_tag_pydantic_schema():
    assert _role_tag("app/schemas/user_schema.py") == "[model]"

def test_role_tag_middleware():
    assert _role_tag("middleware/auth_middleware.go") == "[middleware]"

def test_role_tag_spring_filter():
    assert _role_tag("src/main/java/com/app/CorsFilter.java") == "[middleware]"

# -----------------------------------------------------------------------
# _role_tag - Vue / Svelte / ES Modules (previously not in JS layer)
# -----------------------------------------------------------------------

def test_role_tag_vue_component():
    assert _role_tag("src/components/UserCard.vue") == "[component]"

def test_role_tag_vue_router_view():
    assert _role_tag("src/views/HomeView.vue") == "[container]"

def test_role_tag_vue_composable_hook():
    assert _role_tag("src/composables/useAuth.ts") == "[hook]"

def test_role_tag_vue_composable_dir():
    assert _role_tag("src/composables/auth.ts") == "[hook]"

def test_role_tag_svelte_component():
    assert _role_tag("src/lib/components/Button.svelte") == "[component]"

def test_role_tag_sveltekit_page():
    assert _role_tag("src/routes/users/+page.svelte") == "[route]"

def test_role_tag_mjs_module():
    assert _role_tag("src/utils/format.mjs") == "[util]"

# -----------------------------------------------------------------------
# _role_tag - Elixir / Phoenix
# -----------------------------------------------------------------------

def test_role_tag_phoenix_controller():
    assert _role_tag("lib/myapp_web/controllers/user_controller.ex") == "[controller]"

def test_role_tag_phoenix_channel():
    assert _role_tag("lib/myapp_web/channels/user_channel.ex") == "[service]"

def test_role_tag_elixir_plug():
    assert _role_tag("lib/myapp_web/plugs/auth_plug.ex") == "[middleware]"

# -----------------------------------------------------------------------
# _role_tag - Flutter / Dart
# -----------------------------------------------------------------------

def test_role_tag_flutter_bloc():
    assert _role_tag("lib/features/user/user_bloc.dart") == "[service]"

def test_role_tag_flutter_cubit():
    assert _role_tag("lib/features/user/user_cubit.dart") == "[service]"

def test_role_tag_flutter_provider():
    assert _role_tag("lib/features/user/user_provider.dart") == "[service]"

def test_role_tag_flutter_screen():
    assert _role_tag("lib/screens/home_screen.dart") == "[container]"

def test_role_tag_flutter_widget():
    assert _role_tag("lib/widgets/loading_widget.dart") == "[component]"

def test_role_tag_flutter_repository():
    assert _role_tag("lib/data/user_repository.dart") == "[dao]"

# -----------------------------------------------------------------------
# _role_tag - Swift / iOS
# -----------------------------------------------------------------------

def test_role_tag_swift_viewcontroller():
    assert _role_tag("Sources/App/UserViewController.swift") == "[controller]"

def test_role_tag_swift_viewmodel():
    assert _role_tag("Sources/App/UserViewModel.swift") == "[model]"

def test_role_tag_swift_coordinator():
    assert _role_tag("Sources/App/UserCoordinator.swift") == "[service]"

def test_role_tag_swift_presenter():
    assert _role_tag("Sources/App/UserPresenter.swift") == "[service]"

def test_role_tag_swift_delegate():
    assert _role_tag("Sources/App/UserDelegate.swift") == "[service]"

# -----------------------------------------------------------------------
# _role_tag - Django Channels consumer
# -----------------------------------------------------------------------

def test_role_tag_django_channels_consumer():
    assert _role_tag("chat/consumers.py") == "[service]"

# -----------------------------------------------------------------------
# _role_tag - Next.js / SvelteKit pages
# -----------------------------------------------------------------------

def test_role_tag_nextjs_page():
    # pages/ at project root (no leading slash) must still resolve
    assert _role_tag("pages/dashboard/index.tsx") == "[container]"

def test_role_tag_sveltekit_component():
    # Svelte components in components/ dir get [component]
    assert _role_tag("src/lib/components/Navbar.svelte") == "[component]"

def test_role_tag_sveltekit_route_gets_route():
    # SvelteKit page.svelte inside routes/ is correctly tagged [route]
    # (routes/ = routing directory in SvelteKit, same as Express routes/)
    assert _role_tag("src/routes/admin/page.svelte") == "[route]"


# ---------------------------------------------------------------------------
# New: _sanitize_cluster_filename
# ---------------------------------------------------------------------------

def test_sanitize_normal_label():
    assert _sanitize_cluster_filename("List") == "List"

def test_sanitize_spaces_become_underscores():
    assert _sanitize_cluster_filename("My Cluster") == "My_Cluster"

def test_sanitize_special_chars_removed():
    assert _sanitize_cluster_filename("foo/bar") == "foo_bar"

def test_sanitize_empty_returns_cluster():
    assert _sanitize_cluster_filename("") == "cluster"

def test_sanitize_leading_trailing_underscores_stripped():
    assert _sanitize_cluster_filename("_List_") == "List"


# -----------------------------------------------------------------------
# _community_label: _SKIP_PARTS prevents generic Java package dir names
# as cluster labels
# -----------------------------------------------------------------------

def test_community_label_skips_java_services_package(tmp_path):
    # All files under .../services/dao/impl/ - cluster label must not be "services" or "dao"
    graph = {
        "nodes": [
            {"id": "a", "source_file": "com/example/services/dao/impl/UserDaoImpl.java"},
            {"id": "b", "source_file": "com/example/services/dao/impl/OrderDaoImpl.java"},
        ],
        "edges": [{"source": "a", "target": "b"}],
        "communities": {"0": ["a", "b"]},
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    # Table row exists but label must not be the generic package dir names
    assert "| services |" not in content
    assert "| dao |" not in content
    assert "| impl |" not in content
