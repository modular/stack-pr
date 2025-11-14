from __future__ import annotations

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
        The name of the branch currently checked out, or "HEAD" if the repo is
        in a 'detached HEAD' state

    Raises:
        GitError: if called outside a git repo, or the repo doesn't have any
        commits yet.
    """

    try:
        return get_command_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir
        ).strip()
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


def check_gh_installed() -> None:
    """Check if the gh tool is installed and authenticated.

    Raises:
        GitError: If gh is not available or not authenticated.
    """
    try:
        # Check if gh is installed
        run_shell_command(["gh", "--version"], capture_output=True, quiet=False)

        # Check if gh is authenticated
        auth_status = get_command_output(["gh", "auth", "status"], check=False)
        if "You are not logged into any GitHub hosts" in auth_status:
            raise GitError(
                "'gh' is not authenticated. Please run 'gh auth login' to authenticate."
            )
    except subprocess.CalledProcessError as err:
        raise GitError(
            "'gh' is not installed or not accessible. Please visit https://cli.github.com/ for"
            " installation instructions."
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

    # Parse JSON response properly
    import json

    try:
        response = json.loads(user_query)
        login = response.get("data", {}).get("viewer", {}).get("login")
        if not login:
            raise GitError("Unable to find current github user name")
        return str(login)  # Ensure we return a string
    except json.JSONDecodeError as e:
        raise GitError("Invalid response from GitHub API") from e


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


def is_rebase_in_progress(repo_dir: Path | None = None) -> bool:
    """Check if a rebase operation is currently in progress.

    Args:
        repo_dir: path to the repo. Defaults to the current working directory.

    Returns:
        True if a rebase is in progress, False otherwise.
    """
    git_dir = Path(".git") if repo_dir is None else repo_dir / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
