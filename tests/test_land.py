import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr import cli


def make_args() -> cli.CommonArgs:
    return cli.CommonArgs(
        base="base",
        head="head",
        remote="origin",
        target="main",
        hyperlinks=False,
        verbose=False,
        branch_name_template="$USERNAME/stack/$ID",
        show_tips=False,
        land_disabled=False,
    )


def make_entry(head: str, pr: str, commit_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        head=head,
        pr=pr,
        commit=SimpleNamespace(commit_id=lambda: commit_id),
    )


def test_land_rebases_current_branch_onto_updated_stack_tip(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    stack = [make_entry("stack/1", "pr-1", "commit-1"), make_entry("stack/2", "pr-2", "commit-2")]

    monkeypatch.setattr(cli, "get_current_branch_name", lambda: "feature")
    monkeypatch.setattr(cli, "should_update_local_base", lambda **kwargs: False)
    monkeypatch.setattr(cli, "get_stack", lambda **kwargs: stack)
    monkeypatch.setattr(cli, "set_base_branches", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "print_stack", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "land_pr", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "rebase_pr", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "delete_local_branches", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "branch_exists", lambda branch: False)
    monkeypatch.setattr(
        cli,
        "get_command_output",
        lambda cmd, **kwargs: "rebased-stack-tip\n"
        if cmd == ["git", "rev-parse", "stack/2"]
        else "",
    )
    monkeypatch.setattr(
        cli,
        "is_ancestor",
        lambda ancestor, descendant, *, verbose: ancestor == "commit-2"
        and descendant == "feature",
    )

    def fake_run_shell_command(cmd, *, quiet, check=True, **kwargs):
        commands.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(cli, "run_shell_command", fake_run_shell_command)

    cli.command_land(make_args())

    assert [
        "git",
        "rebase",
        "rebased-stack-tip",
        "feature",
        "--committer-date-is-author-date",
    ] in commands
    assert ["git", "rebase", "stack/2", "feature", "--committer-date-is-author-date"] not in commands
    assert ["git", "rebase", "origin/main", "feature"] not in commands


def test_land_refreshes_remote_target_before_rebasing_current_branch(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    stack = [make_entry("stack/1", "pr-1", "commit-1")]

    monkeypatch.setattr(cli, "get_current_branch_name", lambda: "feature")
    monkeypatch.setattr(cli, "should_update_local_base", lambda **kwargs: False)
    monkeypatch.setattr(cli, "get_stack", lambda **kwargs: stack)
    monkeypatch.setattr(cli, "set_base_branches", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "print_stack", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "land_pr", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "delete_local_branches", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "branch_exists", lambda branch: False)
    monkeypatch.setattr(
        cli,
        "is_ancestor",
        lambda ancestor, descendant, *, verbose: ancestor == "commit-1"
        and descendant == "feature",
    )

    def fake_run_shell_command(cmd, *, quiet, check=True, **kwargs):
        commands.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(cli, "run_shell_command", fake_run_shell_command)

    cli.command_land(make_args())

    assert ["git", "fetch", "--prune", "origin"] in commands
    assert ["git", "rebase", "origin/main", "feature", "--committer-date-is-author-date"] in commands