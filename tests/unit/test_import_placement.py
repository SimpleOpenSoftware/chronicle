from pathlib import Path

from scripts.check_import_placement import check_file


def write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_production_nested_import_requires_explanation(tmp_path):
    path = write(tmp_path / "service.py", "def run():\n    import json\n")
    assert len(check_file(path)) == 1
    write(
        path,
        "def run():\n    # This dependency closes a circular import.\n    import json\n",
    )
    assert not check_file(path)


def test_test_modules_and_fixtures_allow_local_imports(tmp_path):
    for name in (
        "tests/helpers.py",
        "test_service.py",
        "service_test.py",
        "conftest.py",
    ):
        path = write(tmp_path / name, "def run():\n    import json\n")
        assert not check_file(path)
