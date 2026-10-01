"""
The local TimescaleDB and the one CI provisions must be the same database.

`tests/test_timescale_storage.py` runs against whatever is listening on
127.0.0.1:5433. Locally that is the container `scripts/timescaledb.sh up`
starts; in CI it is the `timescaledb` service in `.github/workflows/ci.yml`.
Nothing connects the two, so a version bump or a credential change in one
leaves 92 tests passing against a different engine than the one that gates the
merge -- and a green run on both sides is exactly what that looks like.

REG-0013: the script these six call sites name did not exist at all. The
README documented `bash scripts/timescaledb.sh up`, `src/config.py` and
`src/data/storage.py` pointed at it in comments, and the skip message every
one of those 92 tests printed told the reader to run it. Nothing checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "timescaledb.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def service() -> dict:
    """The `timescaledb` service block from the job that runs the suite."""
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    return workflow["jobs"]["python-tests"]["services"]["timescaledb"]


def _assign(script: str, name: str) -> str:
    match = re.search(rf'^{name}="([^"]+)"$', script, re.MULTILINE)
    assert match, f"{name} is not a plain quoted assignment in timescaledb.sh"
    return match.group(1)


class TestTheScriptExists:
    def test_the_script_every_call_site_names_is_present_and_executable(self):
        assert SCRIPT.is_file(), "scripts/timescaledb.sh is referenced in six places"
        assert SCRIPT.stat().st_mode & 0o111, "scripts/timescaledb.sh is not executable"

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "src/config.py",
            "src/data/storage.py",
            "tests/test_timescale_storage.py",
        ],
    )
    def test_every_scripts_path_named_in_prose_exists(self, path):
        """
        The general form of the defect: a call site naming a file under
        scripts/ that is not there. Keyed on the reference, not on this one
        script, so the next dangling pointer fails here too.
        """
        text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
        referenced = set(re.findall(r"scripts/[A-Za-z0-9_.-]+\.(?:sh|py)", text))
        missing = sorted(r for r in referenced if not (PROJECT_ROOT / r).is_file())
        assert not missing, f"{path} names scripts that do not exist: {missing}"


class TestTheScriptAndCIDescribeTheSameDatabase:
    def test_the_image_matches(self, script, service):
        assert _assign(script, "IMAGE") == service["image"]

    @pytest.mark.parametrize(
        ("variable", "env_key"),
        [
            ("POSTGRES_USER", "POSTGRES_USER"),
            ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
            ("POSTGRES_DB", "POSTGRES_DB"),
        ],
    )
    def test_the_credentials_match(self, script, service, variable, env_key):
        assert _assign(script, variable) == str(service["env"][env_key])

    def test_the_published_port_matches(self, script, service):
        """
        The suite connects to a port, not to a container, so this is the one
        value that decides whether the tests reach the database at all.
        """
        host_port = _assign(script, "HOST_PORT")
        assert [f"{host_port}:5432"] == [str(p) for p in service["ports"]]

    def test_the_default_dsn_agrees_with_both(self, script):
        """
        src/config.py's default is what a developer who never exports
        STORAGE_TIMESCALE_DSN gets. It has to name the database this script
        starts, or `STORAGE_BACKEND=timescale` fails for that developer only.
        """
        from src.config import StorageSettings

        default = StorageSettings.model_fields["timescale_dsn"].default
        user = _assign(script, "POSTGRES_USER")
        password = _assign(script, "POSTGRES_PASSWORD")
        database = _assign(script, "POSTGRES_DB")
        addr = _assign(script, "HOST_ADDR")
        port = _assign(script, "HOST_PORT")
        assert default == f"postgresql://{user}:{password}@{addr}:{port}/{database}"


class TestTheScriptIsSafeToRun:
    def test_the_port_is_bound_to_loopback_only(self, script):
        """
        A bare `--publish 5433:5432` listens on every interface. On a laptop on
        a shared network that publishes the database to it.
        """
        assert '--publish "${HOST_ADDR}:${HOST_PORT}:5432"' in script
        assert _assign(script, "HOST_ADDR") == "127.0.0.1"

    def test_it_stops_on_the_first_error(self, script):
        assert "set -euo pipefail" in script
