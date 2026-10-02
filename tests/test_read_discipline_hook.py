"""
The reading rules the pre-tool-use hook enforces.

At most two lines leave any read, in every session, through every tool, local
or cloud, directly or indirectly; work GitHub already does happens on GitHub;
a file is searched rather than opened; and the pull-request comment channel is
one comment wide -- the latest.

Each rule lives in ``config/command_policy.json`` and is applied by
``.claude/hooks/pre_tool_use.py`` before the tool call runs. The hook is
imported rather than spawned: GOV-016 forbids a subprocess where a call will
do, and every rule here is a pure function of the call and the policy.
``tests/test_pre_tool_use_hook.py`` covers the process-level contract.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = PROJECT_ROOT / ".claude" / "hooks" / "pre_tool_use.py"
SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.json"


@pytest.fixture(scope="module")
def hook():
    """The hook module, loaded once: importing it has no side effects."""
    spec = importlib.util.spec_from_file_location("tb_pre_tool_use", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def policy(hook) -> dict:
    """The real policy file, read once -- these rules are its contract."""
    return hook._load_policy()


@pytest.fixture(scope="module")
def settings() -> dict:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Two lines, every reader
# --------------------------------------------------------------------------


def test_the_cap_is_two_lines(policy):
    assert policy["bounded_output"]["max_declared_lines"] == 2
    assert policy["bounded_output"]["refusal_message"] == "only 2 lines reading is allowed"
    assert policy["direct_file_read"]["max_declared_lines"] == 2


@pytest.mark.parametrize(
    "command",
    [
        # Readers whose default is wider than two lines, with nothing declared.
        "grep -n needle f.py",
        "grep -rn needle .",
        "head f.py",
        "head -10 f.py",
        "tail -5 f.py",
        "tail -n +5 f.py",
        "sed 's/a/b/' f.py",
        "sed -n '1,20p' f.py",
        "awk '{print}' f.py",
        "cut -f1 f.csv",
        "sort f.txt",
        "uniq f.txt",
        "jq . f.json",
        "ls",
        "ls -la src",
        "ps aux",
        "du -sh src",
        "find . -name '*.py'",
        "tree src",
        "journalctl -u svc",
        "head -c 500000 blob.bin",
    ],
)
def test_an_unbounded_read_is_refused(hook, policy, command):
    problems = hook._violations(command, policy)
    assert problems, command
    assert problems[0] == policy["bounded_output"]["refusal_message"]


@pytest.mark.parametrize(
    "command",
    [
        "head -2 f.py",
        "tail -1 f.py",
        "sed -n '10,11p' f.py",
        "sed -n '5p' f.py",
        "grep -m 2 -n needle f.py",
        "grep -m 1 -n needle f.py",
        "grep -c needle f.py",
        "wc -l f.py",
        "ls | head -2",
        "grep -n needle f.py | head -2",
    ],
)
def test_a_two_line_read_is_allowed(hook, policy, command):
    assert hook._violations(command, policy) == []


@pytest.mark.parametrize(
    "command",
    [
        # A context flag is a bound of its own: -A 50 satisfies the match bound
        # and still returns a hundred lines.
        "grep -m 2 -A 50 needle f.py",
        "grep -m 2 -B 20 needle f.py",
        "grep -m 2 -C 10 needle f.py",
        "grep -m 2 --after-context=9 needle f.py",
    ],
)
def test_a_context_flag_cannot_widen_a_bounded_match(hook, policy, command):
    assert hook._violations(command, policy)


def test_a_small_context_flag_is_still_allowed(hook, policy):
    assert hook._violations("grep -m 2 -A 1 needle f.py", policy) == []


# --------------------------------------------------------------------------
# A GitHub report is read two lines at a time
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "gh pr list",
        "gh api repos/o/r/branches/main",
        "git status",
        "git status --porcelain",
        "git log --oneline",
        "git log -n 10",
        "git diff HEAD~1",
        "git branch -a",
        "git show HEAD",
        "git remote -v",
        "gh pr list --limit 5",
        "git status | head -20",
        "curl -s https://api.github.com/repos/o/r/pulls/1",
    ],
)
def test_a_github_read_above_two_lines_is_refused(hook, policy, command):
    assert hook._github_read_bound(command, policy) == (
        policy["github_read_bound"]["message"]
    )


@pytest.mark.parametrize(
    "command",
    [
        "git status --porcelain | head -2",
        "git log --oneline -n 2",
        "git log --oneline | head -1",
        "gh pr list --limit 2",
        "git diff --name-only | wc -l",
        "git status --porcelain | grep -c '^UU'",
        "git show HEAD --stat | sed -n '1,2p'",
    ],
)
def test_a_two_line_github_read_is_allowed(hook, policy, command):
    assert hook._github_read_bound(command, policy) == ""


@pytest.mark.parametrize(
    "command",
    [
        "git add -A",
        'git commit -m "fix the status report"',
        "git push origin HEAD",
        "git fetch origin",
        "git rev-parse HEAD",
        "gh pr create --fill",
        "gh pr comment 381 --body done",
    ],
)
def test_mutating_git_and_gh_commands_are_not_reads(hook, policy, command):
    """Piping `git push` through `head` would hide the failures that matter."""
    assert hook._github_read_bound(command, policy) == ""


@pytest.mark.parametrize(
    "command",
    [
        "grep -m 2 -n 'git status' tests/test_pre_tool_use_hook.py",
        "grep -m 2 -rn 'gh pr view' config/command_policy.json",
        'echo "run git log to see the history"',
        "sed -n '1,2p' notes.md",
    ],
)
def test_mentioning_a_github_read_is_not_performing_one(hook, policy, command):
    """
    The rule is matched per stage against the client that runs it.

    Matched against the whole line instead, `grep -n "git status"` would be
    refused as a GitHub read -- and a guard that refuses searches is a guard
    that gets switched off.
    """
    assert hook._github_read_bound(command, policy) == ""


def test_redirecting_a_github_report_to_a_file_does_not_launder_it(hook, policy):
    assert hook._github_read_bound("git diff > /tmp/x.diff", policy) != ""


def test_ci_log_access_keeps_its_own_message(hook, policy):
    """The CI rule is not an output-size objection and must not be restated as one."""
    problems = hook._violations("gh run view 12345 --log", policy)
    assert problems
    assert problems[0] == policy["ci_log_access"]["message"]


# --------------------------------------------------------------------------
# One comment, the latest
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 381 --json comments",
        "gh pr view 381 --json comments | head -2",
        "gh pr view 381 --json comments -q '.comments[].body' | head -2",
        "gh api repos/o/r/issues/381/comments",
        "gh issue view 5 --json comments -q '.comments[0].body' | head -2",
    ],
)
def test_reading_the_whole_comment_array_is_refused(hook, policy, command):
    """
    `head -1` is not a selector: it returns the OLDEST comment.

    The notice that matters is the last one, so the query has to name it.
    """
    assert hook._comment_read(command, policy) == policy["comment_read"]["message"]
    assert policy["comment_read"]["message"] in hook._violations(command, policy)


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 381 --json comments -q '.comments[-1].body' | head -2",
        "gh pr view 381 --json comments -q '.comments[].body' | tail -2",
        "gh api repos/o/r/issues/381/comments?per_page=1 | head -2",
    ],
)
def test_the_latest_comment_may_be_read(hook, policy, command):
    assert hook._comment_read(command, policy) == ""
    assert hook._violations(command, policy) == []


def test_the_latest_comment_still_obeys_the_two_line_cap(hook, policy):
    """Which comment and how much of it are two separate rules."""
    command = "gh pr view 381 --json comments -q '.comments[-1].body'"
    assert hook._comment_read(command, policy) == ""
    assert hook._violations(command, policy) == [
        policy["github_read_bound"]["message"]
    ]


def test_a_command_that_is_not_a_comment_read_is_untouched(hook, policy):
    assert hook._comment_read("gh pr view 381 --json title | head -2", policy) == ""
    assert hook._comment_read("gh pr comment 381 --body done", policy) == ""


# --------------------------------------------------------------------------
# Work GitHub already does happens on GitHub
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/test_event_bus.py",
        "pytest -q",
        "python3 -m pytest tests/",
        "python -m mypy src/",
        "ruff check src/",
        "ruff format src/",
        "mypy src/engines",
        "pre-commit run --all-files",
        "make test",
        "make lint",
        "npm test",
        "npm ci",
        "npm run build",
        "cargo test",
        "go test ./...",
        "docker build -t x .",
        "coverage run -m pytest",
        "pip install -r requirements.txt",
        "uv pip install -r requirements-dev.txt",
        "python3 .claude/skills/quality-engineering/scripts/qe_gate.py",
        "python3 scripts/check_static_invariants.py",
        "python3 scripts/generate_quality_docs.py --check",
        "cd /repo && pytest",
    ],
)
def test_local_ci_work_is_refused(hook, policy, command):
    expected = policy["local_ci_work"]["message"]
    assert hook._local_ci_work(command, policy) == expected
    assert expected in hook._violations(command, policy)


@pytest.mark.parametrize(
    "command",
    [
        "grep -m 2 -n pytest tests/test_event_bus.py",
        "grep -m 2 -rn 'ruff' requirements-dev.txt",
        "grep -c mypy setup.cfg",
        "python3 scripts/generate_quality_docs.py",
        "python3 scripts/generate_math_docs.py",
        "echo 'make test'",
        "git add tests/test_event_bus.py",
        "grep -m 2 -rn 'qe_gate.py' tests/quality/test_qe_skill.py",
        "grep -m 2 -n 'npm run build' package.json",
    ],
)
def test_searching_for_a_tool_name_is_not_running_it(hook, policy, command):
    """
    Detection is on the executable, not on a substring of the line.

    Matched against the whole line, `grep -rn qe_gate.py tests/` would be
    refused as a local gate run, which would make the repository unsearchable
    by the names of its own scripts.
    """
    assert hook._local_ci_work(command, policy) == ""


@pytest.mark.parametrize(
    "command",
    [
        "python3 .claude/skills/quality-engineering/scripts/qe_gate.py",
        "./scripts/check_static_invariants.py",
        "bash scripts/ci.sh",
        "npm run build",
    ],
)
def test_a_script_is_refused_where_it_is_actually_run(hook, policy, command):
    """An interpreter, a package runner, or the script invoked directly."""
    assert hook._local_ci_work(command, policy) == policy["local_ci_work"]["message"]


def test_the_refusal_names_the_platform_and_nothing_else(policy):
    assert policy["local_ci_work"]["message"] == "use github platform"


# --------------------------------------------------------------------------
# A file is searched, not opened
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat config/command_policy.json",
        "cat .claude/settings.json",
        "less config/command_policy.json",
        "nl -ba config/command_policy.json",
        "strings config/command_policy.json",
        "bat config/command_policy.json",
    ],
)
def test_opening_a_file_whole_is_refused(hook, policy, command):
    expected = policy["direct_file_read"]["message"]
    assert hook._direct_file_read(command, policy) == expected
    assert expected in hook._violations(command, policy)


def test_the_direct_read_refusal_names_the_search(hook, policy):
    """Someone who ran `cat` needs to be told to grep, not told a number."""
    message = hook._direct_file_read("cat config/command_policy.json", policy)
    assert "grep" in message
    assert "sed -n" in message


@pytest.mark.parametrize(
    "command",
    [
        "sed -n '10,11p' config/command_policy.json",
        "grep -m 2 -n 'refusal_message' config/command_policy.json",
        "cat config/command_policy.json | head -2",
        "cat > /tmp/out.txt",
    ],
)
def test_a_bounded_read_or_a_search_is_allowed(hook, policy, command):
    assert hook._direct_file_read(command, policy) == ""


# --------------------------------------------------------------------------
# Every read tool, not just Bash
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        # No bound declared at all.
        ("Read", {"file_path": "/repo/src/engine.py"}),
        ("NotebookRead", {"notebook_path": "/repo/nb.ipynb"}),
        ("Grep", {"pattern": "needle"}),
        ("mcp__Desktop_Commander__read_file", {"path": "/repo/src/engine.py"}),
        ("mcp__Desktop_Commander__start_search", {"pattern": "needle"}),
        ("mcp__terminal__read_terminal", {}),
        # A bound wider than two.
        ("Read", {"file_path": "/repo/src/engine.py", "limit": 3}),
        ("Read", {"file_path": "/repo/src/engine.py", "limit": 500}),
        ("Grep", {"pattern": "needle", "head_limit": 50}),
        ("mcp__Desktop_Commander__read_file", {"path": "/repo/e.py", "length": 400}),
        # A bound that is not a number.
        ("Read", {"file_path": "/repo/src/engine.py", "limit": "all"}),
        ("Read", {"file_path": "/repo/src/engine.py", "limit": 0}),
    ],
)
def test_an_uncapped_read_tool_call_is_refused(hook, policy, tool, tool_input):
    assert hook._read_tool_verdict(tool, tool_input, policy) == (
        policy["direct_file_read"]["message"]
    )


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "/repo/src/engine.py", "limit": 2}),
        ("Read", {"file_path": "/repo/src/engine.py", "limit": 1}),
        ("NotebookRead", {"notebook_path": "/repo/nb.ipynb", "limit": 2}),
        ("Grep", {"pattern": "needle", "head_limit": 2}),
        ("mcp__Desktop_Commander__read_file", {"path": "/repo/e.py", "length": 2}),
        (
            "mcp__Desktop_Commander__read_file",
            {"path": "/repo/e.py", "offset": 40, "length": 2},
        ),
        ("mcp__Desktop_Commander__start_search", {"pattern": "x", "maxResults": 2}),
        ("mcp__Desktop_Commander__get_more_search_results", {"length": 2}),
        ("mcp__terminal__read_terminal", {"lines": 2}),
    ],
)
def test_a_capped_read_tool_call_is_allowed(hook, policy, tool, tool_input):
    assert hook._read_tool_verdict(tool, tool_input, policy) == ""


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        # A negative offset makes Desktop Commander ignore `length` and read a
        # tail of any size, so the bound it declares is not a bound.
        (
            "mcp__Desktop_Commander__read_file",
            {"path": "/repo/e.py", "length": 2, "offset": -20},
        ),
        (
            "mcp__Desktop_Commander__get_more_search_results",
            {"length": 2, "offset": -5},
        ),
        # isUrl reads the target in full whatever the bound says.
        (
            "mcp__Desktop_Commander__read_file",
            {"path": "https://x/y", "length": 2, "isUrl": True},
        ),
        # contextLines multiplies the output of every match returned.
        (
            "mcp__Desktop_Commander__start_search",
            {"pattern": "x", "maxResults": 2, "contextLines": 5},
        ),
    ],
)
def test_a_bound_that_the_tool_ignores_is_refused(hook, policy, tool, tool_input):
    assert hook._read_tool_verdict(tool, tool_input, policy) == (
        policy["direct_file_read"]["message"]
    )


def test_small_context_lines_are_accepted(hook, policy):
    assert hook._read_tool_verdict(
        "mcp__Desktop_Commander__start_search",
        {"pattern": "x", "maxResults": 2, "contextLines": 2},
        policy,
    ) == ""


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__Desktop_Commander__list_directory",
        "mcp__Desktop_Commander__read_process_output",
        "mcp__Desktop_Commander__get_file_info",
        "mcp__Desktop_Commander__list_processes",
    ],
)
def test_a_reader_with_no_bound_field_has_no_allowed_form(hook, policy, tool):
    assert hook._read_tool_verdict(tool, {"path": "."}, policy) == (
        policy["direct_file_read"]["blocked_tool_message"]
    )


def test_tools_that_return_no_content_are_untouched(hook, policy):
    for tool in ("Edit", "Write", "Bash", "mcp__Desktop_Commander__edit_block"):
        assert hook._read_tool_verdict(tool, {"file_path": "x"}, policy) == ""


def test_every_read_tool_is_actually_routed_through_the_hook(settings, policy):
    """A rule the harness never calls is not a rule."""
    matchers = [e["matcher"] for e in settings["hooks"]["PreToolUse"]]
    joined = " ".join(matchers)
    covered = set(policy["direct_file_read"]["read_tools"]) | set(
        policy["direct_file_read"]["blocked_read_tools"]
    )
    missing = [tool for tool in covered if tool not in joined]
    assert not missing, f".claude/settings.json does not route {sorted(missing)}"
    for entry in settings["hooks"]["PreToolUse"]:
        assert "pre_tool_use.py" in entry["hooks"][0]["command"]


# --------------------------------------------------------------------------
# The per-file session budget closes the paging route
# --------------------------------------------------------------------------


@pytest.fixture()
def budget(hook, policy, tmp_path, monkeypatch):
    """The real policy pointed at a throwaway project root and state dir."""
    monkeypatch.setattr(hook, "PROJECT_DIR", tmp_path)
    target = tmp_path / "src" / "big.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n" * 400, encoding="utf-8")
    scoped = json.loads(json.dumps(policy))
    scoped["direct_file_read"]["paging"]["state_dir"] = ".state"
    return scoped, target


def test_paging_one_file_runs_out_of_budget(hook, budget):
    """
    With every read capped at two lines, paging is the only way to read.

    The budget is what still stops a file being walked from its first line to
    its last, so it has to bind before the end of a long file is reached.
    """
    scoped, target = budget
    paging = scoped["direct_file_read"]["paging"]
    paths = [str(target.relative_to(hook.PROJECT_DIR))]
    allowed = 0
    while not hook._budget_verdict(paths, 2, scoped, "s1") and allowed < 200:
        hook._budget_record(paths, 2, scoped, "s1")
        allowed += 1
    assert allowed == paging["max_lines_per_file"] // 2
    assert allowed < 400
    assert hook._budget_verdict(paths, 2, scoped, "s1") == paging["message"]


def test_the_read_count_budget_also_binds(hook, budget):
    scoped, target = budget
    paging = scoped["direct_file_read"]["paging"]
    paths = [str(target.relative_to(hook.PROJECT_DIR))]
    for _ in range(paging["max_reads_per_file"]):
        hook._budget_record(paths, 0, scoped, "s2")
    assert hook._budget_verdict(paths, 0, scoped, "s2") == paging["message"]


def test_budget_is_per_file_and_per_session(hook, budget):
    scoped, target = budget
    paging = scoped["direct_file_read"]["paging"]
    paths = [str(target.relative_to(hook.PROJECT_DIR))]
    hook._budget_record(paths, paging["max_lines_per_file"], scoped, "s3")
    other = hook.PROJECT_DIR / "src" / "small.py"
    other.write_text("y = 2\n", encoding="utf-8")
    assert hook._budget_verdict(["src/small.py"], 2, scoped, "s3") == ""
    assert hook._budget_verdict(paths, 2, scoped, "s4") == ""


def test_searching_never_spends_budget(hook, budget):
    """grep is the route that has to stay open, so it is charged nothing."""
    _scoped, target = budget
    relative = str(target.relative_to(hook.PROJECT_DIR))
    assert hook._read_paths(f"grep -m 2 -n 'x = 1' {relative}") == []
    assert hook._read_paths(f"sed -n '1,2p' {relative}") == [relative]
    assert hook._read_paths(f"head -2 {relative}") == [relative]


def test_a_sed_script_is_not_mistaken_for_a_file(hook, budget):
    _scoped, target = budget
    relative = str(target.relative_to(hook.PROJECT_DIR))
    assert hook._read_paths(f"sed -n '1,2p' {relative}") == [relative]


def test_a_heredoc_body_is_content_not_a_read(hook, budget):
    """
    A document being written is not a file being read.

    Left in, a heredoc that merely contains a pipe produces a stage that looks
    like `head -2 ...`, and whatever path appears on a later line of the
    document gets charged for a read that never happened.
    """
    _scoped, target = budget
    relative = str(target.relative_to(hook.PROJECT_DIR))
    document = f"cat > out.md <<'EOF'\nsee head -2 {relative} for detail\nEOF"
    assert hook._read_paths(document) == []


def test_a_write_costs_no_read_budget(hook, budget):
    _scoped, target = budget
    relative = str(target.relative_to(hook.PROJECT_DIR))
    assert hook._read_paths(f"cat {relative} > /tmp/copy.py") == []


def test_an_unparsed_bound_is_charged_the_ceiling(hook, policy):
    """A read whose bound could not be read is not a free read."""
    assert hook._declared_lines("sed -n '10,11p' f.py", policy) == 2
    assert hook._declared_lines("head -2 f.py", policy) == 2
    assert hook._declared_lines("awk 'NR<9' f.py", policy) == 0


def test_bookkeeping_failure_allows_the_call(hook, policy, tmp_path, monkeypatch):
    """A budget that cannot be written must never be why work stops."""
    monkeypatch.setattr(hook, "PROJECT_DIR", tmp_path / "missing")
    scoped = json.loads(json.dumps(policy))
    scoped["direct_file_read"]["paging"]["state_dir"] = "/proc/nope/state"
    hook._budget_record(["src/x.py"], 2, scoped, "s5")
    assert hook._budget_verdict(["src/x.py"], 2, scoped, "s5") == ""


# --------------------------------------------------------------------------
# An interpreter is a reader too
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 script.py",
        "python3 -c 'print(1)'",
        "node app.js",
        "python3 scripts/generate_quality_docs.py",
        "python3 - <<'PY'\nprint('x')\nPY",
    ],
)
def test_an_interpreter_declares_a_bound_like_any_reader(hook, policy, command):
    """
    A script printing five hundred lines is an indirect read of five hundred
    lines, and the directive covers indirect reads.

    An interpreter was the last route by which an unbounded read could still
    reach the session: it was exempt, and a heredoc counted as silence.
    """
    assert hook._violations(command, policy)


@pytest.mark.parametrize(
    "command",
    [
        "python3 script.py | head -2",
        "python3 script.py > out.log",
        "cat > notes.md <<'EOF'\ncontent\nEOF",
        "python3 - <<'PY' | head -2\nprint('x')\nPY",
    ],
)
def test_a_bounded_or_silent_interpreter_call_is_allowed(hook, policy, command):
    assert hook._violations(command, policy) == []


def test_a_heredoc_alone_does_not_mean_silence(hook):
    """`cat > notes.md <<EOF` is silent because of the redirect, not the heredoc."""
    assert hook._is_write_not_read("cat > notes.md <<'EOF'")
    assert not hook._is_write_not_read("python3 - <<'PY'")


# --------------------------------------------------------------------------
# A quoted shell operator is not a command separator
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "awk 'NR<=190 && /def test/ {print NR}' f.py | head -2",
        "grep -m 2 -n 'a && b' f.py",
        'grep -m 2 -n "x|y" f.py',
        "echo 'a; b' | head -2",
    ],
)
def test_a_quoted_operator_does_not_split_the_command(hook, policy, command):
    """
    Split by plain regex, the `&&` inside an awk script tears the command in
    half and the left fragment carries no bound, so a correctly bounded command
    is refused. A guard that refuses correct commands is one someone turns off.
    """
    assert hook._violations(command, policy) == []


@pytest.mark.parametrize(
    ("command", "count"),
    [
        ("cat f.py && ls", 2),
        ("echo hi; echo there", 2),
        ("a || b", 2),
        ("echo 'x && y'", 1),
        ("echo 'a; b'", 1),
    ],
)
def test_real_operators_still_separate_commands(hook, command, count):
    assert len(hook._commands(command)) == count


@pytest.mark.parametrize(
    ("command", "count"),
    [
        ("grep -m 2 x f | head -2", 2),
        ('echo "a | b"', 1),
        ("cat f | grep x | head -2", 3),
    ],
)
def test_real_pipes_still_separate_stages(hook, command, count):
    assert len(hook._stages(command)) == count


# --------------------------------------------------------------------------
# The rollback path stays open
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "block",
    ["github_read_bound", "local_ci_work", "direct_file_read", "comment_read"],
)
def test_each_rule_can_be_switched_off_in_the_policy(hook, policy, block):
    scoped = json.loads(json.dumps(policy))
    scoped[block]["enabled"] = False
    checks = {
        "github_read_bound": (hook._github_read_bound, "git status"),
        "local_ci_work": (hook._local_ci_work, "pytest -q"),
        "direct_file_read": (hook._direct_file_read, "cat config/command_policy.json"),
        "comment_read": (hook._comment_read, "gh pr view 1 --json comments"),
    }
    check, command = checks[block]
    assert check(command, scoped) == ""


@pytest.mark.parametrize(
    "block",
    ["github_read_bound", "local_ci_work", "direct_file_read", "comment_read"],
)
def test_every_rule_carries_a_message_and_a_rationale(policy, block):
    entry = policy[block]
    assert entry["message"].strip()
    assert entry["_note"].strip()
