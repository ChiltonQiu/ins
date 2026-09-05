"""The declared package list has to match what is on disk.

setuptools is configured with an explicit `packages` list, because `evals`,
`migrations` and `config` sit at the top level and it refuses to guess. An
explicit list silently goes stale: a new subpackage is simply absent from the
wheel, and nothing fails until an install that is not editable tries to import
it.
"""

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def _declared():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return set(data["tool"]["setuptools"]["packages"])


def _on_disk():
    return {
        ".".join(path.parent.relative_to(ROOT).parts)
        for path in (ROOT / "renewal").rglob("__init__.py")
    }


def test_every_package_on_disk_is_declared():
    missing = _on_disk() - _declared()
    assert not missing, f"not in pyproject packages: {sorted(missing)}"


def test_no_declared_package_is_missing_from_disk():
    """A stale name breaks the build rather than the import."""
    phantom = _declared() - _on_disk()
    assert not phantom, f"declared but absent: {sorted(phantom)}"
