"""Regression tests for the Kotlin Spring resolver.

`extract_kotlin_spring_edges` read `m.group(2)` from a one-group regex, raising
IndexError and crashing the whole resolver - silently dropping every
Kotlin-Spring edge (di/route/dao/relation). These tests build a minimal Kotlin
Spring project and assert each edge kind is produced without crashing.
"""
from __future__ import annotations

import tempfile
from pathlib import Path


_FILES = {
    "User.kt": (
        "package com.example\n"
        "import javax.persistence.Entity\n"
        "import javax.persistence.OneToMany\n"
        "@Entity\n"
        "class User(val name: String) {\n"
        "    @OneToMany\n"
        "    val posts: List<Post> = emptyList()\n"
        "}\n"
    ),
    "Post.kt": (
        "package com.example\n"
        "import javax.persistence.Entity\n"
        "@Entity\n"
        "class Post(val title: String)\n"
    ),
    "UserRepository.kt": (
        "package com.example\n"
        "import org.springframework.data.jpa.repository.JpaRepository\n"
        "interface UserRepository : JpaRepository<User, Long>\n"
    ),
    "UserService.kt": (
        "package com.example\n"
        "import org.springframework.stereotype.Service\n"
        "@Service\n"
        "class UserService(val userRepository: UserRepository)\n"
    ),
    "UserController.kt": (
        "package com.example\n"
        "import org.springframework.web.bind.annotation.RestController\n"
        "import org.springframework.web.bind.annotation.GetMapping\n"
        "@RestController\n"
        "class UserController(val userService: UserService) {\n"
        "    @GetMapping(\"/users\")\n"
        "    fun listUsers(): List<User> = emptyList()\n"
        "}\n"
    ),
}


def _build_edges(tmp: Path):
    from icx_engine.graph.parser.extract import extract
    from icx_engine.graph.parser.resolvers.kotlin_spring import extract_kotlin_spring_edges

    proj = tmp / "proj"
    proj.mkdir()
    files = []
    for name, src in _FILES.items():
        p = proj / name
        p.write_text(src, encoding="utf-8")
        files.append(p.resolve())

    with tempfile.TemporaryDirectory() as cache:
        extraction = extract(files, cache_root=Path(cache), parallel=False)
    return extract_kotlin_spring_edges(files, proj.resolve(), extraction)


def test_kotlin_spring_resolver_does_not_crash_and_emits_edges(tmp_path):
    edges = _build_edges(tmp_path)  # must not raise IndexError
    assert edges, "resolver produced no edges (regression: whole resolver crashed)"


def test_kotlin_spring_emits_each_edge_kind(tmp_path):
    edges = _build_edges(tmp_path)
    kinds = {e["relation"] for e in edges}
    # DI (constructor injection), route (@GetMapping), dao (JpaRepository),
    # relation (@OneToMany) must all appear.
    assert "depends_on" in kinds   # UserController->UserService, UserService->UserRepository
    assert "routes" in kinds       # UserController -> listUsers
    assert "dao" in kinds          # UserRepository -> User
    assert "has_relation" in kinds  # User -> Post (@OneToMany)
