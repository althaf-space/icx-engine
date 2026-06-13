from pathlib import Path

from icx_engine.memory.stack_fingerprint import detect_stack


def test_detect_stack_node_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"engines": {"node": "18.17.0"}, "dependencies": {"react": "^18.2.0", "next": "13.4.0"}}',
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"node": "18.17.0"}
    assert result["."]["frameworks"] == {"react": "18.2.0", "next": "13.4.0"}
    assert result["."]["package_manager"] == "npm"


def test_detect_stack_python_pyproject_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n\n'
        '[tool.poetry.dependencies]\nfastapi = "^0.110.0"\n',
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"python": "3.11"}
    assert result["."]["frameworks"] == {"fastapi": "0.110.0"}
    assert result["."]["package_manager"] == "poetry"


def test_detect_stack_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "django==4.2.5\nrequests==2.31.0\n# comment\n",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["frameworks"] == {"django": "4.2.5"}
    assert result["."]["package_manager"] == "pip"


def test_detect_stack_pom_xml_spring_boot(tmp_path):
    (tmp_path / "pom.xml").write_text(
        """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <parent>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.2.1</version>
  </parent>
  <properties>
    <java.version>17</java.version>
  </properties>
</project>""",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"java": "17"}
    assert result["."]["frameworks"] == {"spring-boot": "3.2.1"}
    assert result["."]["package_manager"] == "maven"


def test_detect_stack_pom_xml_no_namespace(tmp_path):
    (tmp_path / "pom.xml").write_text(
        """<project>
  <properties>
    <maven.compiler.source>1.8</maven.compiler.source>
  </properties>
</project>""",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"java": "1.8"}


def test_detect_stack_build_gradle(tmp_path):
    (tmp_path / "build.gradle").write_text(
        "sourceCompatibility = JavaVersion.VERSION_17\n"
        "dependencies {\n"
        "    implementation 'org.springframework.boot:spring-boot-starter-web:3.1.4'\n"
        "}\n",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"java": "17"}
    assert result["."]["frameworks"] == {"spring-boot": "3.1.4"}
    assert result["."]["package_manager"] == "gradle"


def test_detect_stack_cargo_toml(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nedition = "2021"\n',
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"rust": "2021"}
    assert result["."]["package_manager"] == "cargo"


def test_detect_stack_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text(
        "module example.com/myapp\n\ngo 1.21\n",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"go": "1.21"}
    assert result["."]["package_manager"] == "go"


def test_detect_stack_gemfile_rails(tmp_path):
    (tmp_path / "Gemfile").write_text(
        'ruby "3.2.2"\ngem "rails", "~> 7.0.4"\n',
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"ruby": "3.2.2"}
    assert result["."]["frameworks"] == {"rails": "7.0.4"}
    assert result["."]["package_manager"] == "bundler"


def test_detect_stack_composer_json(tmp_path):
    (tmp_path / "composer.json").write_text(
        '{"require": {"php": "^8.2", "laravel/framework": "^10.0"}}',
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"php": "8.2"}
    assert result["."]["frameworks"] == {"laravel": "10.0"}


def test_detect_stack_pubspec_yaml(tmp_path):
    (tmp_path / "pubspec.yaml").write_text(
        "name: myapp\nenvironment:\n  sdk: '>=3.1.0 <4.0.0'\n",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["languages"] == {"dart": "3.1.0"}
    assert result["."]["package_manager"] == "pub"


def test_detect_stack_gradle_variable_version_omitted(tmp_path):
    """A spring-boot version resolved via a Gradle variable is not guessed."""
    (tmp_path / "build.gradle").write_text(
        "ext { springBootVersion = '3.2.0' }\n"
        "dependencies {\n"
        "    implementation \"org.springframework.boot:spring-boot-starter-web:${springBootVersion}\"\n"
        "}\n",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    if "." in result:
        assert "spring-boot" not in result["."].get("frameworks", {})


def test_detect_stack_monorepo_subdirectories(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"express": "4.18.2"}}', encoding="utf-8")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "pom.xml").write_text(
        "<project><properties><java.version>17</java.version></properties></project>",
        encoding="utf-8",
    )
    result = detect_stack(tmp_path)
    assert result["."]["frameworks"] == {"express": "4.18.2"}
    assert result["backend"]["languages"] == {"java": "17"}


def test_detect_stack_no_manifest_returns_empty(tmp_path):
    assert detect_stack(tmp_path) == {}


def test_detect_stack_nonexistent_path_returns_empty():
    assert detect_stack(Path("Z:/does/not/exist")) == {}


def test_detect_stack_skips_noise_dirs(tmp_path):
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "package.json").write_text('{"dependencies": {"lodash": "4.17.21"}}', encoding="utf-8")
    result = detect_stack(tmp_path)
    assert "node_modules" not in result
