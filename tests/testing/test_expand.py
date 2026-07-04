from pathlib import Path
from icx_engine.testing.expand import expand_via_grep, union_rank


def test_grep_finds_importer(tmp_path: Path):
    (tmp_path / "Button.tsx").write_text("export default function Button(){return <button/>;}", encoding="utf-8")
    (tmp_path / "Form.tsx").write_text("import Button from './Button';\n", encoding="utf-8")
    found = expand_via_grep([str(tmp_path / "Button.tsx")], tmp_path)
    norm = {p.replace("\\", "/") for p in found}
    assert any(p.endswith("Form.tsx") for p in norm)


def test_grep_skips_vendor_dirs(tmp_path: Path):
    (tmp_path / "Button.tsx").write_text("export default function Button(){}", encoding="utf-8")
    vendor = tmp_path / "node_modules" / "x"
    vendor.mkdir(parents=True)
    (vendor / "Dep.tsx").write_text("import Button from 'Button';\n", encoding="utf-8")
    found = expand_via_grep([str(tmp_path / "Button.tsx")], tmp_path)
    assert not any("node_modules" in p for p in found)


def test_union_rank_orders_and_tags():
    ranked = union_rank(
        seeds=["a.tsx"],
        graph_files=["a.tsx", "b.tsx"],
        grep_files=["b.tsx", "c.tsx"],
    )
    by_path = dict(ranked)
    assert by_path["a.tsx"] == "seed"
    assert by_path["b.tsx"] == "both"
    assert by_path["c.tsx"] == "grep"
    # seed first
    assert ranked[0][0] == "a.tsx"


def test_union_rank_dedups_path_separators():
    ranked = union_rank(seeds=["src/a.tsx"], graph_files=["src\\a.tsx"], grep_files=[])
    assert len([p for p, _ in ranked if p.replace("\\", "/") == "src/a.tsx"]) == 1


def test_union_rank_both_bucket_when_seed_also_in_grep():
    ranked = union_rank(seeds=["b.tsx"], graph_files=["a.tsx", "b.tsx"], grep_files=["a.tsx", "b.tsx"])
    by_path = dict(ranked)
    assert by_path["a.tsx"] == "both"
    assert by_path["b.tsx"] == "seed"
