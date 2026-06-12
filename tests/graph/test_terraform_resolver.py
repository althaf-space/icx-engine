"""Tests for Phase 7: Terraform / HCL resolver."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.terraform_resolver import resolve_terraform


def _node(node_id, source_file):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": node_id}


class TestResolveterraform:
    def test_no_tf_files_returns_empty(self):
        result = resolve_terraform([], Path("."), {"nodes": []})
        assert result == []

    def test_local_module_source_creates_tf_module_edge(self, tmp_path):
        (tmp_path / "modules" / "vpc").mkdir(parents=True)
        main_tf = tmp_path / "main.tf"
        main_tf.write_text('module "vpc" {\n  source = "./modules/vpc"\n}')
        vpc_main = tmp_path / "modules" / "vpc" / "main.tf"
        vpc_main.write_text('variable "cidr" {}')

        main_rel = str(main_tf.relative_to(tmp_path).as_posix())
        vpc_rel = str(vpc_main.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel), _node("vpc_n", vpc_rel)]
        edges = resolve_terraform([main_tf, vpc_main], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "tf_module" in types
        mod = [e for e in edges if e["type"] == "tf_module"][0]
        assert mod["confidence"] == 0.95

    def test_var_reference_creates_tf_var_ref(self, tmp_path):
        vars_tf = tmp_path / "variables.tf"
        vars_tf.write_text('variable "instance_type" {\n  default = "t3.micro"\n}')
        main_tf = tmp_path / "main.tf"
        main_tf.write_text('resource "aws_instance" "web" {\n  instance_type = var.instance_type\n}')

        vars_rel = str(vars_tf.relative_to(tmp_path).as_posix())
        main_rel = str(main_tf.relative_to(tmp_path).as_posix())
        nodes = [_node("vars_n", vars_rel), _node("main_n", main_rel)]
        edges = resolve_terraform([vars_tf, main_tf], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "tf_var_ref" in types
        vr = [e for e in edges if e["type"] == "tf_var_ref"][0]
        assert vr["confidence"] == 0.80

    def test_data_reference_creates_tf_data_ref(self, tmp_path):
        data_tf = tmp_path / "data.tf"
        data_tf.write_text('data "aws_vpc" "main" {\n  default = true\n}')
        main_tf = tmp_path / "main.tf"
        main_tf.write_text('resource "aws_subnet" "pub" {\n  vpc_id = data.aws_vpc.main.id\n}')

        data_rel = str(data_tf.relative_to(tmp_path).as_posix())
        main_rel = str(main_tf.relative_to(tmp_path).as_posix())
        nodes = [_node("data_n", data_rel), _node("main_n", main_rel)]
        edges = resolve_terraform([data_tf, main_tf], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "tf_data_ref" in types
        dr = [e for e in edges if e["type"] == "tf_data_ref"][0]
        assert dr["confidence"] == 0.85

    def test_resource_dependency_creates_tf_resource_dep(self, tmp_path):
        vpc_tf = tmp_path / "vpc.tf"
        vpc_tf.write_text('resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n}')
        subnet_tf = tmp_path / "subnet.tf"
        subnet_tf.write_text('resource "aws_subnet" "private" {\n  vpc_id = aws_vpc.main.id\n}')

        vpc_rel = str(vpc_tf.relative_to(tmp_path).as_posix())
        subnet_rel = str(subnet_tf.relative_to(tmp_path).as_posix())
        nodes = [_node("vpc_n", vpc_rel), _node("subnet_n", subnet_rel)]
        edges = resolve_terraform([vpc_tf, subnet_tf], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "tf_resource_dep" in types
        rd = [e for e in edges if e["type"] == "tf_resource_dep"][0]
        assert rd["confidence"] == 0.90

    def test_output_block_creates_tf_output_edge(self, tmp_path):
        main_tf = tmp_path / "main.tf"
        main_tf.write_text('resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n}\noutput "vpc_id" {\n  value = aws_vpc.main.id\n}')

        main_rel = str(main_tf.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel)]
        # Output referencing a resource in a DIFFERENT file
        res_tf = tmp_path / "resources.tf"
        res_tf.write_text('resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n}')
        out_tf = tmp_path / "outputs.tf"
        out_tf.write_text('output "vpc_id" {\n  value = aws_vpc.main.id\n}')
        res_rel = str(res_tf.relative_to(tmp_path).as_posix())
        out_rel = str(out_tf.relative_to(tmp_path).as_posix())
        nodes2 = [_node("res_n", res_rel), _node("out_n", out_rel)]
        edges = resolve_terraform([res_tf, out_tf], tmp_path, {"nodes": nodes2})
        types = [e["type"] for e in edges]
        assert "tf_output" in types
        to = [e for e in edges if e["type"] == "tf_output"][0]
        assert to["confidence"] == 0.85

    def test_non_local_module_source_no_edge(self, tmp_path):
        main_tf = tmp_path / "main.tf"
        main_tf.write_text('module "registry_mod" {\n  source = "hashicorp/consul/aws"\n  version = "0.1.0"\n}')
        main_rel = str(main_tf.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel)]
        edges = resolve_terraform([main_tf], tmp_path, {"nodes": nodes})
        # Registry modules don't create edges - no local file to point to
        assert not any(e["type"] == "tf_module" for e in edges)


class TestTerraformEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        (tmp_path / "modules" / "vpc").mkdir(parents=True)
        main_tf = tmp_path / "main.tf"
        main_tf.write_text('module "vpc" {\n  source = "./modules/vpc"\n}')
        vpc_main = tmp_path / "modules" / "vpc" / "main.tf"
        vpc_main.write_text('variable "cidr" {}')
        main_rel = str(main_tf.relative_to(tmp_path).as_posix())
        vpc_rel = str(vpc_main.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel), _node("vpc_n", vpc_rel)]
        edges = resolve_terraform([main_tf, vpc_main], tmp_path, {"nodes": nodes})
        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
