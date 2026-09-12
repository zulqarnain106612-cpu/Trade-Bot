"""
SUP-006 — the container runs as nobody, owns nothing, and carries no builder.

Read as a static check of the `Dockerfile`, which is unusual and deliberate.
The image is built and exercised in CI (`security.yml`, job `container`):
that job runs it read-only with every capability dropped and proves the
application user cannot write to `/app`. What it cannot do is run on every
test invocation, and the properties below are exactly the ones that get
deleted by a well-meaning edit — a `USER root` added to fix a permission
error, a base image switched to a tag "so Dependabot can see it".

So this file pins the decisions, and CI proves they hold in the built image.
Neither is sufficient alone: a Dockerfile that says the right words but does
not build is a posture nobody is running, and a build that passes today tells
you nothing about the line somebody adds tomorrow.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"


@pytest.fixture(scope="module")
def text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def instructions(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


class TestItExists:
    def test_there_is_a_dockerfile_to_check(self):
        # Before PR-009 there was none, which made SUP-006 a rule with no
        # subject -- and the first Dockerfile written under deadline is how a
        # trading process ends up running as root.
        assert DOCKERFILE.exists()


class TestTheImageRunsAsANonRootUser:
    def test_a_user_is_declared(self, instructions):
        assert any(i.startswith("USER ") for i in instructions)

    def test_the_last_user_is_not_root(self, instructions):
        users = [i.split(maxsplit=1)[1].strip() for i in instructions if i.startswith("USER ")]
        assert users, "no USER instruction"
        final = users[-1]
        assert final not in ("root", "0", "0:0")
        assert not final.startswith("root")

    def test_the_user_is_a_numeric_id(self, instructions):
        # A name has to be resolved against /etc/passwd, which a Kubernetes
        # `runAsNonRoot` check cannot do -- it reads the numeric id and
        # refuses to start an image whose user it cannot prove is non-zero.
        final = [i for i in instructions if i.startswith("USER ")][-1]
        assert re.fullmatch(r"USER \d+:\d+", final), final

    def test_the_account_has_no_login_shell(self, text):
        assert "nologin" in text


class TestTheProcessCannotRewriteItself:
    def test_the_application_tree_is_not_owned_by_the_runtime_user(self, text):
        # Root-owned code, non-root process: an exploit that lands in the
        # container cannot persist a modification of the code it exploited.
        assert "--chown=root:root" in text
        assert "--chown=tradebot" not in text
        assert "--chown=10001" not in text


class TestTheBuilderDoesNotShip:
    def test_the_build_is_multi_stage(self, instructions):
        froms = [i for i in instructions if i.startswith("FROM ")]
        assert len(froms) >= 2, "a single-stage build ships its own toolchain"

    def test_the_runtime_stage_copies_from_the_build_stage(self, text):
        assert "COPY --from=build" in text

    def test_no_compiler_or_package_manager_is_installed_in_the_runtime_stage(self, text):
        runtime = text.split("AS runtime", 1)[-1]
        for smell in ("apt-get install", "build-essential", "gcc", "git "):
            assert smell not in runtime, f"runtime stage installs {smell!r}"


class TestTheBaseIsPinnedAndMinimal:
    def test_every_base_is_pinned_by_digest(self, instructions):
        # Same reasoning as SHA-pinning an action: a tag is a pointer somebody
        # else can move, and the thing it moves is the code you run as a
        # process holding exchange credentials.
        for line in (i for i in instructions if i.startswith("FROM ")):
            image = line.split()[1]
            assert "@sha256:" in image, f"unpinned base image: {image}"
            digest = image.split("@sha256:", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{64}", digest), f"malformed digest: {image}"

    def test_the_base_is_a_slim_variant(self, text):
        # Recorded in the comment beside the digest, since the digest itself
        # says nothing readable.
        assert "slim" in text

    def test_the_python_version_matches_the_repository(self, text):
        declared = (REPO / ".python-version").read_text(encoding="utf-8").strip()
        assert f"python:{declared}" in text, (
            f"the image comment must name python:{declared}, the version CI installs"
        )


class TestRuntimeHygiene:
    def test_pyc_files_are_not_written(self, text):
        # A read-only filesystem plus bytecode writing is a startup failure
        # that looks like an application bug.
        assert "PYTHONDONTWRITEBYTECODE=1" in text

    def test_output_is_unbuffered(self, text):
        # Buffered stdout in a container is a log that stops at the moment
        # the process dies, which is the moment the log mattered.
        assert "PYTHONUNBUFFERED=1" in text

    def test_there_is_a_health_check(self, text):
        assert "HEALTHCHECK" in text

    def test_the_entrypoint_is_exec_form(self, text):
        # Shell form wraps the process in /bin/sh, which swallows SIGTERM --
        # so an orderly shutdown becomes a kill, mid-order.
        match = re.search(r"^ENTRYPOINT\s+(.+)$", text, re.M)
        assert match, "no ENTRYPOINT"
        assert match.group(1).strip().startswith("["), "ENTRYPOINT must be exec form"


class TestCiActuallyBuildsAndScansIt:
    @pytest.fixture(scope="class")
    def workflow(self) -> str:
        return (REPO / ".github/workflows/security.yml").read_text(encoding="utf-8")

    def test_the_image_is_built_in_ci(self, workflow):
        assert "docker build" in workflow

    def test_the_non_root_assertion_runs_in_ci(self, workflow):
        assert "10001:10001" in workflow

    def test_the_read_only_run_is_exercised(self, workflow):
        assert "--read-only" in workflow
        assert "--cap-drop=ALL" in workflow
        assert "no-new-privileges" in workflow

    def test_the_image_is_scanned(self, workflow):
        assert "trivy-action@" in workflow
        assert "HIGH,CRITICAL" in workflow

    def test_the_scan_fails_the_job(self, workflow):
        # A scanner reporting findings into a log nobody reads is not a gate.
        assert 'exit-code: "1"' in workflow

    def test_the_container_job_is_in_the_gate(self, workflow):
        needs = re.search(r"needs: \[(.+?)\]", workflow.split("Security gate")[1])
        assert needs and "container" in needs.group(1)
