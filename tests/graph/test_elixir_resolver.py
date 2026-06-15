from pathlib import Path

from icx_engine.graph.parser.resolvers.elixir_resolver import resolve_elixir


def _node(node_id, name, source_file):
    return {"id": node_id, "name": name, "source_file": source_file}


class TestElixirResolverNoOp:
    def test_no_ex_files_returns_empty(self, tmp_path):
        assert resolve_elixir([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        ex_file = tmp_path / "foo.ex"
        ex_file.write_text("defmodule Foo do\nend\n", encoding="utf-8")
        assert resolve_elixir([ex_file], tmp_path, {"nodes": []}) == []


class TestElixirAlias:
    def test_alias_resolves_to_defmodule(self, tmp_path):
        bar_file = tmp_path / "bar.ex"
        bar_file.write_text("defmodule MyApp.Bar do\n  def run, do: :ok\nend\n", encoding="utf-8")
        foo_file = tmp_path / "foo.ex"
        foo_file.write_text(
            "defmodule MyApp.Foo do\n  alias MyApp.Bar\n  def run, do: :ok\nend\n", encoding="utf-8",
        )

        nodes = [
            _node("foo_n", "MyApp.Foo", "foo.ex"),
            _node("bar_n", "MyApp.Bar", "bar.ex"),
        ]
        edges = resolve_elixir([foo_file, bar_file], tmp_path, {"nodes": nodes})
        alias_edges = [e for e in edges if e["relation"] == "elixir_alias"]
        assert ("foo_n", "bar_n") in [(e["source"], e["target"]) for e in alias_edges]


class TestElixirUse:
    def test_use_resolves_to_declaring_module(self, tmp_path):
        behaviour_file = tmp_path / "behaviour.ex"
        behaviour_file.write_text("defmodule MyApp.Behaviour do\nend\n", encoding="utf-8")
        impl_file = tmp_path / "impl.ex"
        impl_file.write_text(
            "defmodule MyApp.Impl do\n  use MyApp.Behaviour\nend\n", encoding="utf-8",
        )

        nodes = [
            _node("impl_n", "MyApp.Impl", "impl.ex"),
            _node("behaviour_n", "MyApp.Behaviour", "behaviour.ex"),
        ]
        edges = resolve_elixir([impl_file, behaviour_file], tmp_path, {"nodes": nodes})
        use_edges = [e for e in edges if e["relation"] == "elixir_use"]
        assert ("impl_n", "behaviour_n") in [(e["source"], e["target"]) for e in use_edges]


class TestElixirCalls:
    def test_aliased_module_call_resolves_to_declaring_file(self, tmp_path):
        bar_file = tmp_path / "bar.ex"
        bar_file.write_text("defmodule MyApp.Bar do\n  def run, do: :ok\nend\n", encoding="utf-8")
        foo_file = tmp_path / "foo.ex"
        foo_file.write_text(
            "defmodule MyApp.Foo do\n  alias MyApp.Bar\n\n  def run do\n    Bar.run()\n  end\nend\n",
            encoding="utf-8",
        )

        nodes = [
            _node("foo_n", "MyApp.Foo", "foo.ex"),
            _node("bar_n", "MyApp.Bar", "bar.ex"),
        ]
        edges = resolve_elixir([foo_file, bar_file], tmp_path, {"nodes": nodes})
        call_edges = [e for e in edges if e["relation"] == "elixir_calls"]
        assert ("foo_n", "bar_n") in [(e["source"], e["target"]) for e in call_edges]
