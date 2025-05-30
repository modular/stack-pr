from __future__ import annotations

import re
import string
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from stack_pr.shell_commands import get_command_output, run_shell_command


class GitError(Exception):
    pass


# Git constants
GIT_NOT_A_REPO_ERROR = 128
GIT_SHA_LENGTH = 40


@dataclass
class GitConfig:
    """
    Configuration for git operations.
    """

    username_override: str | None = None

    def set_username_override(self, username: str | None) -> None:
        """Override username for testing purposes. Call with None to reset."""
        self.username_override = username


# Create a singleton instance
git_config = GitConfig()


def is_full_git_sha(s: str) -> bool:
    """Return True if the given string is a valid full git SHA.

    The string needs to consist of 40 lowercase hex characters.

    """
    if len(s) != GIT_SHA_LENGTH:
        return False

    digits = set(string.hexdigits.lower())
    return all(c in digits for c in s)


def branch_exists(branch: str, repo_dir: Path | None = None) -> bool:
    """Returns whether a branch with the given name exists.

    Args:
        branch: branch name as a string.
        repo_dir: path to the repo. Defaults to the current working directory.

    Returns:
        True if the branch exists, False otherwise.

    Raises:
        GitError: if called outside a git repo.
    """
    proc = run_shell_command(
        ["git", "show-ref", "-q", f"refs/heads/{branch}"],
        stderr=subprocess.DEVNULL,
        cwd=repo_dir,
        check=False,
        quiet=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitError("Not inside a valid git repository.")


def get_current_branch_name(repo_dir: Path | None = None) -> str:
    """Returns the name of the branch currently checked out.

    Args:
        repo_dir: path to the repo. Defaults to the current working directory.

    Returns:
        The name of the branch currently checked out, or a hash if the repo is
        in a 'detached HEAD' state

    Raises:
        GitError: if called outside a git repo, or the repo doesn't have any
        commits yet.
    """

    try:
        result = get_command_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir
        ).strip()
        if result == "HEAD":
            # Detached HEAD state, return a hash so we can cleanup after ourselves properly, since
            #    git checkout HEAD
            # is a no-op.
            result = get_command_output(
                ["git", "rev-parse", "HEAD"], cwd=repo_dir
            ).strip()
        return result
    except subprocess.CalledProcessError as e:
        if e.returncode == GIT_NOT_A_REPO_ERROR:
            raise GitError("Not inside a valid git repository.") from e
        raise


def get_uncommitted_changes(
    repo_dir: Path | None = None,
) -> dict[str, list[str]]:
    """Return a dictionary of uncommitted changes.

    Args:
        repo_dir: path to the repo. Defaults to the current working directory.

    Returns:
        A dictionary with keys as described in
        https://git-scm.com/docs/git-status#_short_format and values as lists
        of the corresponding changes, each change either in the format "PATH",
        or "ORIG_PATH -> PATH".

    Raises:
        GitError: if called outside a git repo.
    """
    try:
        out = get_command_output(["git", "status", "--porcelain"], cwd=repo_dir)
    except subprocess.CalledProcessError as e:
        if e.returncode == GIT_NOT_A_REPO_ERROR:
            raise GitError("Not inside a valid git repository.") from None
        raise

    changes: dict[str, list[str]] = {}
    for line in out.splitlines():
        # First two chars are the status, changed path starts at 4th character.
        changes.setdefault(line[:2], []).append(line[3:])
    return changes


# TODO: enforce this as a module dependency
def check_gh_installed() -> None:
    """Check if the gh tool is installed.

    Raises:
        GitError if gh is not available.
    """

    try:
        run_shell_command(["gh"], capture_output=True, quiet=False)
    except subprocess.CalledProcessError as err:
        raise GitError(
            "'gh' is not installed. Please visit https://cli.github.com/ for"
            " installation instuctions."
        ) from err


def get_gh_username() -> str:
    """Return the current github username.

    If username_override is set, it will be used instead of the actual username.

    Returns:
        Current github username as a string.

    Raises:
        GitError: if called outside a git repo.
    """
    if git_config.username_override is not None:
        return git_config.username_override

    user_query = get_command_output(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            "owner=UserCurrent",
            "-f",
            "query=query{viewer{login}}",
        ]
    )

    # Extract the login name.
    m = re.search(r"\"login\":\"(.*?)\"", user_query)
    if not m:
        raise GitError("Unable to find current github user name")

    return m.group(1)


def get_changed_files(
    base: str | None = None, repo_dir: Path | None = None
) -> Sequence[Path]:
    """Get the list of files changed between this commit and the base commit.

    Returns:
        A list of Path objects that correspond to the changed files.
    """
    get_file_changes = [
        "git",
        "diff",
        "--name-only",
        base if base is not None else "main",
        "HEAD",
    ]
    result = get_command_output(get_file_changes, cwd=repo_dir)
    return [Path(r) for r in result.split("\n")]


def get_changed_dirs(
    base: str | None = None, repo_dir: Path | None = None
) -> set[Path]:
    """Get the list of top-level directories changed between this commit
       and the base commit.

    Returns:
        A list of Path objects that correspond to the directories that have
        files changed.
    """
    return {Path(file.parts[0]) for file in get_changed_files(base, repo_dir)}
