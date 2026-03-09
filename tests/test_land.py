import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from stack_pr.cli import CommonArgs, command_land


def test_command_land_skips_target_rebase_checked_out_in_other_worktree(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    logs: list[str] = []

    monkeypatch.setattr("stack_pr.cli.get_current_branch_name", lambda: "feature")
    monkeypatch.setattr(
        "stack_pr.cli.should_update_local_base",
        lambda **_: False,
    )
    monkeypatch.setattr("stack_pr.cli.get_stack", lambda **_: [object()])
    monkeypatch.setattr("stack_pr.cli.set_base_branches", lambda *_, **__: None)
    monkeypatch.setattr("stack_pr.cli.print_stack", lambda *_, **__: None)
    monkeypatch.setattr("stack_pr.cli.verify", lambda *_, **__: None)
    monkeypatch.setattr("stack_pr.cli.land_pr", lambda *_, **__: None)
    monkeypatch.setattr("stack_pr.cli.delete_local_branches", lambda *_, **__: None)
    monkeypatch.setattr("stack_pr.cli.branch_exists", lambda _: True)
    monkeypatch.setattr(
        "stack_pr.cli.branch_checked_out_in_other_worktree",
        lambda branch: Path("/tmp/other-worktree") if branch == "main" else None,
    )
    monkeypatch.setattr(
        "stack_pr.cli.run_shell_command",
        lambda cmd, quiet=True: commands.append(cmd),
    )
    monkeypatch.setattr("stack_pr.cli.log", lambda msg, level=1: logs.append(msg))

    command_land(
        CommonArgs(
            base="main",
            head="feature",
            remote="origin",
            target="main",
            hyperlinks=False,
            verbose=False,
            branch_name_template="$USERNAME/stack/$ID",
            show_tips=True,
            land_disabled=False,
        )
    )

    assert ["git", "rebase", "origin/main", "main"] not in commands
    assert ["git", "rebase", "origin/main", "feature"] in commands
    assert any("Skipping update of local branch main" in msg for msg in logs)


def test_command_land_uses_remote_target_when_base_checked_out_in_other_worktree(
    monkeypatch,
) -> None:
    stack_bases: list[str] = []
    logs: list[str] = []
    updated_bases: list[str] = []

    monkeypatch.setattr("stack_pr.cli.get_current_branch_name", lambda: "feature")
    monkeypatch.setattr(
        "stack_pr.cli.should_update_local_base",
        lambda **_: True,
    )
    monkeypatch.setattr("stack_pr.cli.branch_exists", lambda branch: branch == "main")
    monkeypatch.setattr(
        "stack_pr.cli.branch_checked_out_in_other_worktree",
        lambda branch: Path("/tmp/other-worktree") if branch == "main" else None,
    )
    monkeypatch.setattr(
        "stack_pr.cli.update_local_base",
        lambda *, base, remote, target, verbose: updated_bases.append(base),
    )
    monkeypatch.setattr(
        "stack_pr.cli.get_stack",
        lambda *, base, head, verbose: stack_bases.append(base) or [],
    )
    monkeypatch.setattr("stack_pr.cli.log", lambda msg, level=1: logs.append(msg))

    command_land(
        CommonArgs(
            base="main",
            head="feature",
            remote="origin",
            target="main",
            hyperlinks=False,
            verbose=False,
            branch_name_template="$USERNAME/stack/$ID",
            show_tips=True,
            land_disabled=False,
        )
    )

    assert updated_bases == []
    assert stack_bases == ["origin/main"]
    assert any("Using origin/main as stack base." in msg for msg in logs)
