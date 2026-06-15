"""Tests for the Angular resolver: NgModule declarations/imports and constructor DI edges."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.angular_resolver import resolve_angular


def _node(node_id, source_file, name=None):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": name or node_id}


class TestNoTsFiles:
    def test_no_ts_files_returns_empty(self):
        result = resolve_angular([], Path("."), {"nodes": []})
        assert result == []


class TestNgModuleDeclarations:
    def test_declarations_array_creates_angular_declares_edge(self, tmp_path):
        component_file = tmp_path / "app.component.ts"
        component_file.write_text(
            "import { Component } from '@angular/core';\n\n"
            "@Component({\n"
            "  selector: 'app-root'\n"
            "})\n"
            "export class AppComponent {}\n"
        )
        module_file = tmp_path / "app.module.ts"
        module_file.write_text(
            "import { NgModule } from '@angular/core';\n"
            "import { AppComponent } from './app.component';\n\n"
            "@NgModule({\n"
            "  declarations: [\n"
            "    AppComponent\n"
            "  ],\n"
            "  imports: []\n"
            "})\n"
            "export class AppModule {}\n"
        )

        component_rel = str(component_file.relative_to(tmp_path).as_posix())
        module_rel = str(module_file.relative_to(tmp_path).as_posix())
        nodes = [_node("module_n", module_rel, "AppModule"), _node("component_n", component_rel, "AppComponent")]
        edges = resolve_angular([module_file, component_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "angular_declares" in types
        decl_edge = [e for e in edges if e["type"] == "angular_declares"][0]
        assert decl_edge["confidence"] == 0.85
        assert decl_edge["source"] == "module_n"
        assert decl_edge["target"] == "component_n"

    def test_unresolved_declaration_creates_no_edge(self, tmp_path):
        module_file = tmp_path / "app.module.ts"
        module_file.write_text(
            "import { NgModule } from '@angular/core';\n"
            "import { BrowserModule } from '@angular/platform-browser';\n\n"
            "@NgModule({\n"
            "  declarations: [\n"
            "    SomeExternalComponent\n"
            "  ],\n"
            "  imports: []\n"
            "})\n"
            "export class AppModule {}\n"
        )

        module_rel = str(module_file.relative_to(tmp_path).as_posix())
        nodes = [_node("module_n", module_rel, "AppModule")]
        edges = resolve_angular([module_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "angular_declares" for e in edges)


class TestNestedBracketImports:
    def test_imports_with_nested_call_and_trailing_module_resolves(self, tmp_path):
        shared_file = tmp_path / "shared.module.ts"
        shared_file.write_text(
            "import { NgModule } from '@angular/core';\n\n"
            "@NgModule({\n"
            "  declarations: [],\n"
            "  imports: []\n"
            "})\n"
            "export class SharedModule {}\n"
        )
        app_module_file = tmp_path / "app.module.ts"
        app_module_file.write_text(
            "import { NgModule } from '@angular/core';\n"
            "import { RouterModule } from '@angular/router';\n"
            "import { SharedModule } from './shared.module';\n\n"
            "@NgModule({\n"
            "  declarations: [],\n"
            "  imports: [\n"
            "    RouterModule.forRoot([\n"
            "      { path: '', component: AppComponent }\n"
            "    ]),\n"
            "    SharedModule\n"
            "  ]\n"
            "})\n"
            "export class AppModule {}\n"
        )

        shared_rel = str(shared_file.relative_to(tmp_path).as_posix())
        app_rel = str(app_module_file.relative_to(tmp_path).as_posix())
        nodes = [_node("app_n", app_rel, "AppModule"), _node("shared_n", shared_rel, "SharedModule")]
        edges = resolve_angular([app_module_file, shared_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "angular_imports" in types
        import_edge = [e for e in edges if e["type"] == "angular_imports"][0]
        assert import_edge["source"] == "app_n"
        assert import_edge["target"] == "shared_n"


class TestNgModuleImports:
    def test_imports_array_creates_angular_imports_edge(self, tmp_path):
        shared_file = tmp_path / "shared.module.ts"
        shared_file.write_text(
            "import { NgModule } from '@angular/core';\n\n"
            "@NgModule({\n"
            "  declarations: [],\n"
            "  imports: []\n"
            "})\n"
            "export class SharedModule {}\n"
        )
        app_module_file = tmp_path / "app.module.ts"
        app_module_file.write_text(
            "import { NgModule } from '@angular/core';\n"
            "import { SharedModule } from './shared.module';\n\n"
            "@NgModule({\n"
            "  declarations: [],\n"
            "  imports: [\n"
            "    SharedModule\n"
            "  ]\n"
            "})\n"
            "export class AppModule {}\n"
        )

        shared_rel = str(shared_file.relative_to(tmp_path).as_posix())
        app_rel = str(app_module_file.relative_to(tmp_path).as_posix())
        nodes = [_node("app_n", app_rel, "AppModule"), _node("shared_n", shared_rel, "SharedModule")]
        edges = resolve_angular([app_module_file, shared_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "angular_imports" in types
        import_edge = [e for e in edges if e["type"] == "angular_imports"][0]
        assert import_edge["confidence"] == 0.85
        assert import_edge["source"] == "app_n"
        assert import_edge["target"] == "shared_n"


class TestConstructorDI:
    def test_constructor_injection_creates_angular_di_edge(self, tmp_path):
        service_file = tmp_path / "data.service.ts"
        service_file.write_text(
            "import { Injectable } from '@angular/core';\n\n"
            "@Injectable({\n"
            "  providedIn: 'root'\n"
            "})\n"
            "export class DataService {\n"
            "  getData() { return []; }\n"
            "}\n"
        )
        component_file = tmp_path / "user.component.ts"
        component_file.write_text(
            "import { Component } from '@angular/core';\n"
            "import { DataService } from './data.service';\n\n"
            "@Component({\n"
            "  selector: 'app-user'\n"
            "})\n"
            "export class UserComponent {\n"
            "  constructor(private dataService: DataService) {}\n"
            "}\n"
        )

        service_rel = str(service_file.relative_to(tmp_path).as_posix())
        component_rel = str(component_file.relative_to(tmp_path).as_posix())
        nodes = [_node("component_n", component_rel, "UserComponent"), _node("service_n", service_rel, "DataService")]
        edges = resolve_angular([component_file, service_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "angular_di" in types
        di_edge = [e for e in edges if e["type"] == "angular_di"][0]
        assert di_edge["confidence"] == 0.75
        assert di_edge["source"] == "component_n"
        assert di_edge["target"] == "service_n"

    def test_multiple_classes_in_one_file_each_resolve_own_constructor(self, tmp_path):
        service_a_file = tmp_path / "a.service.ts"
        service_a_file.write_text(
            "import { Injectable } from '@angular/core';\n\n"
            "@Injectable({ providedIn: 'root' })\n"
            "export class AServiceClass {\n"
            "  doA() { return 'a'; }\n"
            "}\n"
        )
        service_b_file = tmp_path / "b.service.ts"
        service_b_file.write_text(
            "import { Injectable } from '@angular/core';\n\n"
            "@Injectable({ providedIn: 'root' })\n"
            "export class BServiceClass {\n"
            "  doB() { return 'b'; }\n"
            "}\n"
        )
        multi_file = tmp_path / "multi.ts"
        multi_file.write_text(
            "import { AServiceClass } from './a.service';\n"
            "import { BServiceClass } from './b.service';\n\n"
            "export class FirstComponent {\n"
            "  constructor(private a: AServiceClass) {}\n"
            "}\n\n"
            "export class SecondComponent {\n"
            "  constructor(private b: BServiceClass) {}\n"
            "}\n"
        )

        a_rel = str(service_a_file.relative_to(tmp_path).as_posix())
        b_rel = str(service_b_file.relative_to(tmp_path).as_posix())
        multi_rel = str(multi_file.relative_to(tmp_path).as_posix())
        nodes = [
            _node("first_n", multi_rel, "FirstComponent"),
            _node("second_n", multi_rel, "SecondComponent"),
            _node("a_n", a_rel, "AServiceClass"),
            _node("b_n", b_rel, "BServiceClass"),
        ]
        edges = resolve_angular([multi_file, service_a_file, service_b_file], tmp_path, {"nodes": nodes})

        di_edges = [e for e in edges if e["type"] == "angular_di"]
        pairs = {(e["source"], e["target"]) for e in di_edges}
        assert ("first_n", "a_n") in pairs
        assert ("second_n", "b_n") in pairs

    def test_injection_of_type_not_in_project_creates_no_edge(self, tmp_path):
        component_file = tmp_path / "user.component.ts"
        component_file.write_text(
            "import { Component } from '@angular/core';\n"
            "import { HttpClient } from '@angular/common/http';\n\n"
            "@Component({\n"
            "  selector: 'app-user'\n"
            "})\n"
            "export class UserComponent {\n"
            "  constructor(private http: HttpClient) {}\n"
            "}\n"
        )

        component_rel = str(component_file.relative_to(tmp_path).as_posix())
        nodes = [_node("component_n", component_rel, "UserComponent")]
        edges = resolve_angular([component_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "angular_di" for e in edges)


class TestAngularEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        component_file = tmp_path / "app.component.ts"
        component_file.write_text(
            "import { Component } from '@angular/core';\n\n"
            "@Component({\n"
            "  selector: 'app-root'\n"
            "})\n"
            "export class AppComponent {}\n"
        )
        module_file = tmp_path / "app.module.ts"
        module_file.write_text(
            "import { NgModule } from '@angular/core';\n"
            "import { AppComponent } from './app.component';\n\n"
            "@NgModule({\n"
            "  declarations: [\n"
            "    AppComponent\n"
            "  ],\n"
            "  imports: []\n"
            "})\n"
            "export class AppModule {}\n"
        )

        component_rel = str(component_file.relative_to(tmp_path).as_posix())
        module_rel = str(module_file.relative_to(tmp_path).as_posix())
        nodes = [_node("module_n", module_rel, "AppModule"), _node("component_n", component_rel, "AppComponent")]
        edges = resolve_angular([module_file, component_file], tmp_path, {"nodes": nodes})

        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
        assert all(e["resolver"] == "angular" for e in edges)


class TestTemplateUrl:
    def test_template_url_creates_angular_template_edge(self, tmp_path):
        html_file = tmp_path / "app.component.html"
        html_file.write_text("<h1>Hello</h1>\n")
        component_file = tmp_path / "app.component.ts"
        component_file.write_text(
            "import { Component } from '@angular/core';\n\n"
            "@Component({\n"
            "  selector: 'app-root',\n"
            "  templateUrl: './app.component.html'\n"
            "})\n"
            "export class AppComponent {}\n"
        )

        html_rel = str(html_file.relative_to(tmp_path).as_posix())
        component_rel = str(component_file.relative_to(tmp_path).as_posix())
        nodes = [_node("component_n", component_rel, "AppComponent"), _node("html_n", html_rel)]
        edges = resolve_angular([component_file, html_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "angular_template" in types
        tpl_edge = [e for e in edges if e["type"] == "angular_template"][0]
        assert tpl_edge["confidence"] == 0.90
        assert tpl_edge["source"] == "component_n"
        assert tpl_edge["target"] == "html_n"

    def test_unresolved_template_url_creates_no_edge(self, tmp_path):
        component_file = tmp_path / "app.component.ts"
        component_file.write_text(
            "import { Component } from '@angular/core';\n\n"
            "@Component({\n"
            "  selector: 'app-root',\n"
            "  templateUrl: './missing.component.html'\n"
            "})\n"
            "export class AppComponent {}\n"
        )

        component_rel = str(component_file.relative_to(tmp_path).as_posix())
        nodes = [_node("component_n", component_rel, "AppComponent")]
        edges = resolve_angular([component_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "angular_template" for e in edges)


class TestSelectorUsage:
    def test_selector_in_template_creates_angular_selector_edge(self, tmp_path):
        user_component_file = tmp_path / "user.component.ts"
        user_component_file.write_text(
            "import { Component } from '@angular/core';\n\n"
            "@Component({\n"
            "  selector: 'app-user'\n"
            "})\n"
            "export class UserComponent {}\n"
        )
        host_component_file = tmp_path / "app.component.ts"
        host_component_file.write_text(
            "import { Component } from '@angular/core';\n\n"
            "@Component({\n"
            "  selector: 'app-root'\n"
            "})\n"
            "export class AppComponent {}\n"
        )
        html_file = tmp_path / "app.component.html"
        html_file.write_text("<div>\n  <app-user></app-user>\n</div>\n")

        user_rel = str(user_component_file.relative_to(tmp_path).as_posix())
        host_rel = str(host_component_file.relative_to(tmp_path).as_posix())
        html_rel = str(html_file.relative_to(tmp_path).as_posix())
        nodes = [
            _node("user_n", user_rel, "UserComponent"),
            _node("host_n", host_rel, "AppComponent"),
            _node("html_n", html_rel),
        ]
        edges = resolve_angular([user_component_file, host_component_file, html_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "angular_selector" in types
        sel_edge = [e for e in edges if e["type"] == "angular_selector"][0]
        assert sel_edge["confidence"] == 0.75
        assert sel_edge["source"] == "html_n"
        assert sel_edge["target"] == "user_n"

    def test_unknown_tag_creates_no_selector_edge(self, tmp_path):
        component_file = tmp_path / "app.component.ts"
        component_file.write_text(
            "import { Component } from '@angular/core';\n\n"
            "@Component({\n"
            "  selector: 'app-root'\n"
            "})\n"
            "export class AppComponent {}\n"
        )
        html_file = tmp_path / "app.component.html"
        html_file.write_text("<div>\n  <some-unknown-widget></some-unknown-widget>\n</div>\n")

        component_rel = str(component_file.relative_to(tmp_path).as_posix())
        html_rel = str(html_file.relative_to(tmp_path).as_posix())
        nodes = [_node("component_n", component_rel, "AppComponent"), _node("html_n", html_rel)]
        edges = resolve_angular([component_file, html_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "angular_selector" for e in edges)
