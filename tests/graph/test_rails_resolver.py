"""Tests for Phase 5: Ruby on Rails convention resolver."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.rails_resolver import resolve_rails, _snake, _pascal


def _node(node_id, source_file):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": node_id}


class TestHelpers:
    def test_snake_from_pascal(self):
        assert _snake("UserProfile") == "user_profile"
        assert _snake("User") == "user"

    def test_pascal_from_snake(self):
        assert _pascal("user_profile") == "UserProfile"
        assert _pascal("searchable") == "Searchable"


class TestResolveRails:
    def test_no_app_controllers_returns_empty(self, tmp_path):
        ruby_file = tmp_path / "something.rb"
        ruby_file.write_text("puts 'hello'")
        result = resolve_rails([ruby_file], tmp_path, {"nodes": []})
        assert result == []

    def test_controller_to_views(self, tmp_path):
        (tmp_path / "app" / "controllers").mkdir(parents=True)
        (tmp_path / "app" / "views" / "users").mkdir(parents=True)
        ctrl = tmp_path / "app" / "controllers" / "users_controller.rb"
        ctrl.write_text("class UsersController < ApplicationController; end")
        view = tmp_path / "app" / "views" / "users" / "index.erb"
        view.write_text("<h1>Users</h1>")

        ctrl_rel = str(ctrl.relative_to(tmp_path).as_posix())
        view_rel = str(view.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", ctrl_rel), _node("view_n", view_rel)]
        edges = resolve_rails([ctrl, view], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "rails_view" in types
        rv = [e for e in edges if e["type"] == "rails_view"][0]
        assert rv["confidence"] == 0.85

    def test_model_to_controller(self, tmp_path):
        (tmp_path / "app" / "controllers").mkdir(parents=True)
        (tmp_path / "app" / "models").mkdir(parents=True)
        ctrl = tmp_path / "app" / "controllers" / "users_controller.rb"
        ctrl.write_text("class UsersController; end")
        model = tmp_path / "app" / "models" / "user.rb"
        model.write_text("class User < ActiveRecord::Base; end")

        ctrl_rel = str(ctrl.relative_to(tmp_path).as_posix())
        model_rel = str(model.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", ctrl_rel), _node("model_n", model_rel)]
        edges = resolve_rails([ctrl, model], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "rails_model_controller" in types

    def test_routes_to_controller(self, tmp_path):
        (tmp_path / "app" / "controllers").mkdir(parents=True)
        (tmp_path / "config").mkdir(parents=True)
        ctrl = tmp_path / "app" / "controllers" / "users_controller.rb"
        ctrl.write_text("class UsersController; end")
        routes = tmp_path / "config" / "routes.rb"
        routes.write_text("Rails.application.routes.draw do\n  resources :users\nend")

        ctrl_rel = str(ctrl.relative_to(tmp_path).as_posix())
        routes_rel = str(routes.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", ctrl_rel), _node("routes_n", routes_rel)]
        edges = resolve_rails([ctrl, routes], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "rails_route" in types
        rr = [e for e in edges if e["type"] == "rails_route"][0]
        assert rr["confidence"] == 0.90

    def test_ar_usage_in_controller(self, tmp_path):
        (tmp_path / "app" / "controllers").mkdir(parents=True)
        (tmp_path / "app" / "models").mkdir(parents=True)
        ctrl = tmp_path / "app" / "controllers" / "users_controller.rb"
        ctrl.write_text("def index\n  @users = User.where(active: true)\nend")
        model = tmp_path / "app" / "models" / "user.rb"
        model.write_text("class User < ActiveRecord::Base; end")

        ctrl_rel = str(ctrl.relative_to(tmp_path).as_posix())
        model_rel = str(model.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", ctrl_rel), _node("model_n", model_rel)]
        edges = resolve_rails([ctrl, model], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "rails_ar_usage" in types
        ar = [e for e in edges if e["type"] == "rails_ar_usage"][0]
        assert ar["confidence"] == 0.70

    def test_concern_include(self, tmp_path):
        (tmp_path / "app" / "controllers").mkdir(parents=True)
        (tmp_path / "app" / "models" / "concerns").mkdir(parents=True)
        model = tmp_path / "app" / "models" / "user.rb"
        model.write_text("class User\n  include Searchable\nend")
        concern = tmp_path / "app" / "models" / "concerns" / "searchable.rb"
        concern.write_text("module Searchable; end")
        # Need a controller to activate Rails detection
        ctrl = tmp_path / "app" / "controllers" / "users_controller.rb"
        ctrl.write_text("class UsersController; end")

        model_rel = str(model.relative_to(tmp_path).as_posix())
        concern_rel = str(concern.relative_to(tmp_path).as_posix())
        ctrl_rel = str(ctrl.relative_to(tmp_path).as_posix())
        nodes = [_node("model_n", model_rel), _node("concern_n", concern_rel), _node("ctrl_n", ctrl_rel)]
        edges = resolve_rails([model, concern, ctrl], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "rails_concern" in types
        rc = [e for e in edges if e["type"] == "rails_concern"][0]
        assert rc["confidence"] == 0.80


class TestRailsEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        (tmp_path / "app" / "controllers").mkdir(parents=True)
        (tmp_path / "app" / "models").mkdir(parents=True)
        ctrl = tmp_path / "app" / "controllers" / "users_controller.rb"
        ctrl.write_text("class UsersController < ApplicationController; end")
        model = tmp_path / "app" / "models" / "user.rb"
        model.write_text("class User < ApplicationRecord; end")
        ctrl_rel = str(ctrl.relative_to(tmp_path).as_posix())
        model_rel = str(model.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", ctrl_rel), _node("model_n", model_rel)]
        edges = resolve_rails([ctrl, model], tmp_path, {"nodes": nodes})
        if edges:
            assert all("relation" in e for e in edges)
            assert all(e["relation"] == e["type"] for e in edges)
