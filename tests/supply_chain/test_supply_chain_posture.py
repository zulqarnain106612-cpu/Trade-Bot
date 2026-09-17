"""
SUP-001..SUP-007 — the supply chain, checked where it is visible.

Every failure in this class is a configuration fact, not a runtime behaviour.
A workflow with `permissions: write-all` runs correctly. An action referenced
by a tag runs correctly until the tag is moved. A release workflow triggered
by `push` publishes correctly, including the push that was a merged dependency
bump nobody read. None of it shows up in a test that exercises the program,
which is why these are assertions about `.github/` and the `Dockerfile`.

The checks live in `scripts/check_supply_chain.py` so CI and this suite run
exactly the same code; what this file adds is the other half — that the
checker itself detects the violations it claims to, driven against synthetic
workflows. A scanner nobody has shown a positive to is a scanner that passes
because it finds nothing anywhere.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts.check_supply_chain import (
    FORK_TRIGGERS,
    PRODUCTION_WORKFLOWS,
    check_action_pinning,
    check_bot_cannot_reach_production,
    check_dependency_surveillance,
    check_fork_secret_isolation,
    check_permissions,
    run_all,
    workflows,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return tmp_path


def write_workflow(root: Path, name: str, body: str) -> Path:
    path = root / ".github" / "workflows" / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


class TestThisRepository:
    """The posture as it actually stands. These are the assertions that matter."""

    def test_every_workflow_declares_least_privilege(self):
        assert [str(f) for f in check_permissions(REPO)] == []

    def test_every_action_is_pinned_to_a_sha(self):
        assert [str(f) for f in check_action_pinning(REPO)] == []

    def test_no_fork_pull_request_can_reach_a_secret(self):
        assert [str(f) for f in check_fork_secret_isolation(REPO)] == []

    def test_dependency_surveillance_covers_every_ecosystem(self):
        assert [str(f) for f in check_dependency_surveillance(REPO)] == []

    def test_no_bot_can_trigger_a_release(self):
        assert [str(f) for f in check_bot_cannot_reach_production(REPO)] == []

    def test_the_whole_posture_is_clean(self):
        assert [str(f) for f in run_all(REPO)] == []

    def test_there_are_workflows_to_check(self):
        # The scan passes vacuously on an empty directory, which is the way
        # this entire file could stop meaning anything without failing.
        assert len(workflows(REPO)) >= 7

    def test_a_release_workflow_exists_to_be_constrained(self):
        assert (REPO / ".github/workflows/release.yml").exists()
        assert "release.yml" in PRODUCTION_WORKFLOWS


class TestThePermissionsCheckDetects:
    def test_a_workflow_with_no_permissions_block(self, fake_repo):
        write_workflow(
            fake_repo,
            "bad.yml",
            """
            name: Bad
            on: [push]
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hi
            """,
        )
        assert check_permissions(fake_repo)

    @pytest.mark.parametrize("blanket", ["write-all", "read-all"])
    def test_a_blanket_grant(self, fake_repo, blanket):
        write_workflow(
            fake_repo,
            "bad.yml",
            f"""
            name: Bad
            on: [push]
            permissions: {blanket}
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hi
            """,
        )
        assert check_permissions(fake_repo)

    def test_an_unexplained_top_level_write_scope(self, fake_repo):
        write_workflow(
            fake_repo,
            "bad.yml",
            """
            name: Bad
            on: [push]
            permissions:
              contents: write
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hi
            """,
        )
        assert check_permissions(fake_repo)

    def test_a_read_only_workflow_passes(self, fake_repo):
        write_workflow(
            fake_repo,
            "good.yml",
            """
            name: Good
            on: [push]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hi
            """,
        )
        assert check_permissions(fake_repo) == []


class TestThePinningCheckDetects:
    @pytest.mark.parametrize(
        "ref",
        [
            "actions/checkout@v4",
            "actions/checkout@main",
            "actions/checkout",
            "actions/checkout@1234567",  # short SHA is still movable-looking
        ],
    )
    def test_an_unpinned_action(self, fake_repo, ref):
        write_workflow(
            fake_repo,
            "bad.yml",
            f"""
            name: Bad
            on: [push]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - uses: {ref}
            """,
        )
        assert check_action_pinning(fake_repo)

    def test_a_sha_with_no_version_comment(self, fake_repo):
        # Pinned but unreadable: nobody can tell what version it is, so nobody
        # updates it, so it never moves even when it should.
        write_workflow(
            fake_repo,
            "bad.yml",
            """
            name: Bad
            on: [push]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
            """,
        )
        assert check_action_pinning(fake_repo)

    def test_a_local_composite_action_is_not_flagged(self, fake_repo):
        write_workflow(
            fake_repo,
            "good.yml",
            """
            name: Good
            on: [push]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - uses: ./.github/actions/setup
            """,
        )
        assert check_action_pinning(fake_repo) == []


class TestTheForkCheckDetects:
    def test_pull_request_target(self, fake_repo):
        # The single most dangerous trigger in GitHub Actions: base-repository
        # secrets plus fork-controlled code.
        write_workflow(
            fake_repo,
            "bad.yml",
            """
            name: Bad
            on: [pull_request_target]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: echo hi
            """,
        )
        assert check_fork_secret_isolation(fake_repo)

    def test_an_unguarded_secret_on_a_pull_request(self, fake_repo):
        write_workflow(
            fake_repo,
            "bad.yml",
            """
            name: Bad
            on: [pull_request]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: deploy.sh
                    env:
                      TOKEN: ${{ secrets.PRODUCTION_TOKEN }}
            """,
        )
        assert check_fork_secret_isolation(fake_repo)

    def test_a_fork_guarded_secret_passes(self, fake_repo):
        write_workflow(
            fake_repo,
            "good.yml",
            """
            name: Good
            on: [pull_request]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                if: github.event.pull_request.head.repo.fork == false
                steps:
                  - run: deploy.sh
                    env:
                      TOKEN: ${{ secrets.PRODUCTION_TOKEN }}
            """,
        )
        assert check_fork_secret_isolation(fake_repo) == []

    def test_the_run_token_is_not_treated_as_a_stored_secret(self, fake_repo):
        # GITHUB_TOKEN's power is bounded by `permissions:`, which SUP-001
        # already checks. Flagging it would make this check noise.
        write_workflow(
            fake_repo,
            "good.yml",
            """
            name: Good
            on: [pull_request]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: gh pr view
                    env:
                      GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
            """,
        )
        assert check_fork_secret_isolation(fake_repo) == []

    def test_both_fork_triggers_are_in_scope(self):
        assert frozenset({"pull_request", "pull_request_target"}) == FORK_TRIGGERS


class TestTheProductionCheckDetects:
    @pytest.mark.parametrize("trigger", ["push", "pull_request", "schedule"])
    def test_a_release_workflow_with_an_automatic_trigger(self, fake_repo, trigger):
        body = {
            "push": "on: [push]",
            "pull_request": "on: [pull_request]",
            "schedule": 'on:\n  schedule:\n    - cron: "0 0 * * *"',
        }[trigger]
        write_workflow(
            fake_repo,
            "release.yml",
            f"""
            name: Release
            {body}
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: publish.sh
            """,
        )
        assert check_bot_cannot_reach_production(fake_repo)

    def test_a_tag_and_dispatch_release_passes(self, fake_repo):
        write_workflow(
            fake_repo,
            "release.yml",
            """
            name: Release
            on:
              push:
                tags:
                  - "v*.*.*"
              workflow_dispatch:
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: publish.sh
            """,
        )
        # `push` restricted to tags is not the same trigger as `push` on a
        # branch: a merged dependency bump does not create a version tag, so
        # publishing still takes a deliberate act.
        assert check_bot_cannot_reach_production(fake_repo) == []

    def test_a_tag_trigger_that_also_takes_branches_is_still_flagged(self, fake_repo):
        # The loophole the tag allowance would otherwise open.
        write_workflow(
            fake_repo,
            "release.yml",
            """
            name: Release
            on:
              push:
                tags:
                  - "v*.*.*"
                branches: [main]
            permissions:
              contents: read
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: publish.sh
            """,
        )
        assert check_bot_cannot_reach_production(fake_repo)


class TestTheSurveillanceCheckDetects:
    def test_a_missing_config(self, fake_repo):
        assert check_dependency_surveillance(fake_repo)

    def test_a_config_missing_an_ecosystem(self, fake_repo):
        (fake_repo / "requirements.txt").write_text("pytest\n")
        (fake_repo / ".github" / "dependabot.yml").write_text(
            "version: 2\nupdates:\n  - package-ecosystem: pip\n"
            '    directory: "/"\n    schedule:\n      interval: weekly\n'
        )
        # github-actions is always required; this config omits it.
        assert check_dependency_surveillance(fake_repo)

    def test_a_config_with_no_schedule(self, fake_repo):
        (fake_repo / ".github" / "dependabot.yml").write_text(
            'version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: "/"\n'
        )
        assert check_dependency_surveillance(fake_repo)
