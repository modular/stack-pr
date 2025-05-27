# stack-pr: a tool for working with stacked PRs on github.
#
# ---------------
# stack-pr submit
# ---------------
#
# Semantics:
#  1. Find merge-base (the most recent commit from 'main' in the current branch)
#  2. For each commit since merge base do:
#       a. If it doesnt have stack info:
#           - create a new head branch for it
#           - create a new PR for it
#           - base branch will be the previous commit in the stack
#       b. If it has stack info: verify its correctness.
#  3. Make sure all commits in the stack are annotated with stack info
#  4. Push all the head branches
#
# If 'submit' succeeds, you'll get all commits annotated with links to the
# corresponding PRs and names of the head branches. All the branches will be
# pushed to remote, and PRs are properly created and interconnected. Base
# branch of each PR will be the head branch of the previous PR, or 'main' for
# the first PR in the stack.
#
# -------------
# stack-pr land
# -------------
#
# Semantics:
#  1. Find merge-base (the most recent commit from 'main' in the current branch)
#  2. Check that all commits in the stack have stack info. If not, bail.
#  3. Check that the stack info is valid. If not, bail.
#  4. For each commit in the stack, from oldest to newest:
#     - set base branch to point to main
#     - merge the corresponding PR
#
# If 'land' succeeds, all the PRs from the stack will be merged into 'main',
# all the corresponding remote and local branches deleted.
#
# ----------------
# stack-pr abandon
# ----------------
#
# Semantics:
# For all commits in the stack that have valid stack-info:
# Close the corresponding PR, delete the remote and local branch, remove the
# stack-info from commit message.
#
# ===----------------------------------------------------------------------=== #

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import cache
from logging import getLogger
from pathlib import Path
from re import Pattern
from subprocess import SubprocessError

from stack_pr.git import (
    branch_exists,
    check_gh_installed,
    get_current_branch_name,
    get_gh_username,
    get_uncommitted_changes,
)
from stack_pr.shell_commands import (
    get_command_output,
    run_shell_command,
)

logger = getLogger(__name__)

# A bunch of regexps for parsing commit messages and PR descriptions
RE_RAW_COMMIT_ID = re.compile(r"^(?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_AUTHOR = re.compile(
    r"^author (?P<author>(?P<name>[^<]+?) <(?P<email>[^>]+)>)", re.MULTILINE
)
RE_RAW_PARENT = re.compile(r"^parent (?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_TREE = re.compile(r"^tree (?P<tree>.+)$", re.MULTILINE)
RE_RAW_COMMIT_MSG_LINE = re.compile(r"^    (?P<line>.*)$", re.MULTILINE)

# stack-info: PR: https://github.com/modularml/test-ghstack/pull/30, branch: mvz/stack/7
RE_STACK_INFO_LINE = re.compile(
    r"\n^stack-info: PR: (.+), branch: (.+)\n?", re.MULTILINE
)
RE_PR_TOC = re.compile(
    r"^Stacked PRs:\r?\n(^ \* (__->__)?#\d+\r?\n)*\r?\n", re.MULTILINE
)

# Delimeter for PR body
CROSS_LINKS_DELIMETER = "--- --- ---"

# ===----------------------------------------------------------------------=== #
# Error message templates
# ===----------------------------------------------------------------------=== #
ERROR_CANT_UPDATE_META = """Couldn't update stack metadata for
    {e}
"""
ERROR_CANT_CREATE_PR = """Could not create a new PR for:
    {e}

Failed trying to execute {cmd}
"""
ERROR_CANT_REBASE = """Could not rebase the PR on '{target}'. Failed to land PR:
    {e}

Failed trying to execute {cmd}
"""
ERROR_CANT_CHECKOUT_REMOTE_BRANCH = """Could not checkout remote branch '{e.head}'. Failed to land PR:
    {e}

Failed trying to execute {cmd}
"""
ERROR_STACKINFO_MISSING = """A stack entry is missing some information:
    {e}

If you wanted to land a part of the stack, please use -B and -H options to
specify base and head revisions.
If you wanted to land the entire stack, please use 'submit' first.
If you hit this error trying to submit, please report a bug!
"""
ERROR_STACKINFO_BAD_LINK = """Bad PR link in stack metadata!
    {e}
"""
ERROR_STACKINFO_MALFORMED_RESPONSE = """Malformed response from GH!

Returned json object is missing a field {required_field}
PR info from github: {d}

Failed verification for:
     {e}
"""
ERROR_STACKINFO_PR_NOT_OPEN = """Associated PR is not in 'OPEN' state!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_NUMBER_MISMATCH = """PR number on github mismatches PR number in stack metadata!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_HEAD_MISMATCH = """Head branch name on github mismatches head branch name in stack metadata!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_BASE_MISMATCH = """Base branch name on github mismatches base branch name in stack metadata!
     {e}

If you are trying land the stack, please update it first by calling 'submit'.

PR info from github: {d}
"""
ERROR_STACKINFO_PR_NOT_MERGEABLE = """Associated PR is not mergeable on GitHub!
     {e}

Please fix the issues on GitHub.

PR info from github: {d}
"""
ERROR_REPO_DIRTY = """There are uncommitted changes.

Please commit or stash them before working with stacks.
"""
UPDATE_STACK_TIP = """
If you'd like to push your local changes first, you can use the following command to update the stack:
  $ stack-pr export -B {top_commit}~{stack_size} -H {top_commit}"""
EXPORT_STACK_TIP = """
You can use the following command to do that:
  $ stack-pr export -B {top_commit}~{stack_size} -H {top_commit}
"""
LAND_STACK_TIP = """
To land it, you could run:
  $ stack-pr land -B {top_commit}~{stack_size} -H {top_commit}

If you'd like to land stack except the top N commits, you could use the following command:
  $ stack-pr land -B {top_commit}~{stack_size} -H {top_commit}~N

If you prefer to merge via the github web UI, please don't forget to edit commit message on the merge page!
If you use the default commit message filled by the web UI, links to other PRs from the stack will be included in the commit message.
"""


# ===----------------------------------------------------------------------=== #
# Class to work with git commit contents
# ===----------------------------------------------------------------------=== #
@dataclass
class CommitHeader:
    """
    Represents the information extracted from `git rev-list --header`
    """

    # The unparsed output from git rev-list --header
    raw_header: str

    def _search_group(self, regex: Pattern[str], group: str) -> str:
        m = regex.search(self.raw_header)
        if m is None:
            raise ValueError(
                f"Required field '{group}' not found in commit header: {self.raw_header}"
            )
        return m.group(group)

    def tree(self) -> str:
        return self._search_group(RE_RAW_TREE, "tree")

    def title(self) -> str:
        return self._search_group(RE_RAW_COMMIT_MSG_LINE, "line")

    def commit_id(self) -> str:
        return self._search_group(RE_RAW_COMMIT_ID, "commit")

    def parents(self) -> list[str]:
        return [m.group("commit") for m in RE_RAW_PARENT.finditer(self.raw_header)]

    def author(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "author")

    def author_name(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "name")

    def author_email(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "email")

    def commit_msg(self) -> str:
        return "\n".join(
            m.group("line") for m in RE_RAW_COMMIT_MSG_LINE.finditer(self.raw_header)
        )


# ===----------------------------------------------------------------------=== #
# Class to work with PR stack entries
# ===----------------------------------------------------------------------=== #
@dataclass
class StackEntry:
    """
    Represents an entry in a stack of PRs and contains associated info, such as
    linked PR, head and base branches, original git commit.
    """

    commit: CommitHeader
    _pr: str | None = None
    _base: str | None = None
    _head: str | None = None
    need_update: bool = False

    @property
    def pr(self) -> str:
        if self._pr is None:
            raise ValueError("pr is not set")
        return self._pr

    @pr.setter
    def pr(self, pr: str) -> None:
        self._pr = pr

    def has_pr(self) -> bool:
        return self._pr is not None

    @property
    def head(self) -> str:
        if self._head is None:
            raise ValueError("head is not set")
        return self._head

    @head.setter
    def head(self, head: str) -> None:
        self._head = head

    def has_head(self) -> bool:
        return self._head is not None

    @property
    def base(self) -> str | None:
        return self._base

    @base.setter
    def base(self, base: str | None) -> None:
        self._base = base

    def has_base(self) -> bool:
        return self._base is not None

    def has_missing_info(self) -> bool:
        return None in (self._pr, self._head, self._base)

    def pprint(self, *, links: bool) -> str:
        s = b(self.commit.commit_id()[:8])
        pr_string = None
        pr_string = blue("#" + last(self.pr)) if self.has_pr() else red("no PR")
        branch_string = None
        if self._head or self._base:
            head_str = green(self._head) if self._head else red(str(self._head))
            base_str = green(self._base) if self._base else red(str(self._base))
            branch_string = f"'{head_str}' -> '{base_str}'"
        if pr_string or branch_string:
            s += " ("
        s += pr_string if pr_string else ""
        if branch_string:
            s += ", " if pr_string else ""
            s += branch_string
        if pr_string or branch_string:
            s += ")"
        s += ": " + self.commit.title()

        if links and self.has_pr():
            s = link(self.pr, s)

        return s

    def __repr__(self) -> str:
        return self.pprint(links=False)

    def read_metadata(self) -> None:
        self.commit.commit_msg()
        x = RE_STACK_INFO_LINE.search(self.commit.commit_msg())
        if not x:
            return
        self.pr = x.group(1)
        self.head = x.group(2)


# ===----------------------------------------------------------------------=== #
# Utils for color printing
# ===----------------------------------------------------------------------=== #


class ShellColors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def b(s: str) -> str:
    return ShellColors.BOLD + s + ShellColors.ENDC


def h(s: str) -> str:
    return ShellColors.HEADER + s + ShellColors.ENDC


def green(s: str) -> str:
    return ShellColors.OKGREEN + s + ShellColors.ENDC


def blue(s: str) -> str:
    return ShellColors.OKBLUE + s + ShellColors.ENDC


def red(s: str) -> str:
    return ShellColors.FAIL + s + ShellColors.ENDC


# https://gist.github.com/egmontkob/eb114294efbcd5adb1944c9f3cb5feda
def link(location: str, text: str) -> str:
    """
    Emits a link to the terminal using the terminal hyperlink specification.

    Does not properly implement file URIs. Only use with web URIs.
    """
    return f"\033]8;;{location}\033\\{text}\033]8;;\033\\"


def error(msg: str) -> None:
    print(red("\nERROR: ") + msg)


def log(msg: str, *, level: int = 1) -> None:
    if level <= 1:
        print(msg)
    elif level == 1:
        logger.info(msg)
    elif level >= 2:  # noqa: PLR2004
        logger.debug(msg)


# ===----------------------------------------------------------------------=== #
# Common utility functions
# ===----------------------------------------------------------------------=== #
def split_header(s: str) -> list[CommitHeader]:
    return [CommitHeader(h) for h in s.split("\0")[:-1]]


def last(ref: str, sep: str = "/") -> str:
    return ref.rsplit(sep, 1)[-1]


# TODO: Move to 'modular.utils.git'
def is_ancestor(commit1: str, commit2: str, *, verbose: bool) -> bool:
    """
    Returns true if 'commit1' is an ancestor of 'commit2'.
    """
    # TODO: We need to check returncode of this command more carefully, as the
    # command simply might fail (rc != 0 and rc != 1).
    p = run_shell_command(
        ["git", "merge-base", "--is-ancestor", commit1, commit2],
        check=False,
        quiet=not verbose,
    )
    return p.returncode == 0


def is_repo_clean() -> bool:
    """
    Returns true if there are no uncommitted changes in the repo.
    """
    changes = get_uncommitted_changes()
    changes.pop("??", [])  # We don't care about untracked files
    return not bool(changes)


def get_stack(base: str, head: str, *, verbose: bool) -> list[StackEntry]:
    if not is_ancestor(base, head, verbose=verbose):
        error(
            f"{base} is not an ancestor of {head}.\n"
            "Could not find commits for the stack."
        )
        sys.exit(1)

    # Find list of commits since merge base.
    st: list[StackEntry] = []
    stack = (
        split_header(
            get_command_output(["git", "rev-list", "--header", "^" + base, head])
        )
    )[::-1]

    for i in range(len(stack)):
        entry = StackEntry(stack[i])
        st.append(entry)

    for e in st:
        e.read_metadata()
    return st


def set_base_branches(st: list[StackEntry], target: str) -> None:
    prev_branch: str | None = target
    for e in st:
        e.base, prev_branch = prev_branch, e.head


def verify(st: list[StackEntry], *, check_base: bool = False) -> None:
    log(h("Verifying stack info"))
    for index, e in enumerate(st):
        if e.has_missing_info():
            error(ERROR_STACKINFO_MISSING.format(**locals()))
            raise RuntimeError

        if len(e.pr.split("/")) == 0 or not last(e.pr).isnumeric():
            error(ERROR_STACKINFO_BAD_LINK.format(**locals()))
            raise RuntimeError

        ghinfo = get_command_output(
            [
                "gh",
                "pr",
                "view",
                e.pr,
                "--json",
                "baseRefName,headRefName,number,state,body,title,url,mergeStateStatus",
            ]
        )
        d = json.loads(ghinfo)
        for required_field in ["state", "number", "baseRefName", "headRefName"]:
            if required_field not in d:
                error(ERROR_STACKINFO_MALFORMED_RESPONSE.format(**locals()))
                raise RuntimeError

        if d["state"] != "OPEN":
            error(ERROR_STACKINFO_PR_NOT_OPEN.format(**locals()))
            raise RuntimeError

        if int(last(e.pr)) != d["number"]:
            error(ERROR_STACKINFO_PR_NUMBER_MISMATCH.format(**locals()))
            raise RuntimeError

        if e.head != d["headRefName"]:
            error(ERROR_STACKINFO_PR_HEAD_MISMATCH.format(**locals()))
            raise RuntimeError

        # 'Base' branch might diverge when the stack is modified (e.g. when a
        # new commit is added to the middle of the stack). It is not an issue
        # if we're updating the stack (i.e. in 'submit'), but it is an issue if
        # we are trying to land it.
        if check_base and e.base != d["baseRefName"]:
            error(ERROR_STACKINFO_PR_BASE_MISMATCH.format(**locals()))
            raise RuntimeError

        # The first entry on the stack needs to be actually mergeable on GitHub.
        if (
            check_base
            and index == 0
            and d["mergeStateStatus"] not in ["CLEAN", "UNKNOWN", "UNSTABLE"]
        ):
            error(ERROR_STACKINFO_PR_NOT_MERGEABLE.format(**locals()))
            raise RuntimeError


def print_stack(st: list[StackEntry], *, links: bool, level: int = 1) -> None:
    log(b("Stack:"), level=level)
    for e in reversed(st):
        log("   * " + e.pprint(links=links), level=level)


def draft_bitmask_type(value: str) -> list[bool]:
    # Validate that only 0s and 1s are present
    if value and not set(value).issubset({"0", "1"}):
        raise argparse.ArgumentTypeError("Bitmask must only contain 0s and 1s.")

    # Convert to list of booleans
    return [bool(int(bit)) for bit in value]


# ===----------------------------------------------------------------------=== #
# SUBMIT
# ===----------------------------------------------------------------------=== #
def add_or_update_metadata(e: StackEntry, *, needs_rebase: bool, verbose: bool) -> bool:
    if needs_rebase:
        if not e.has_base() or not e.has_head():
            error("Stack entry has no base or head branch")
            raise RuntimeError

        run_shell_command(
            [
                "git",
                "rebase",
                e.base or "",
                e.head or "",
                "--committer-date-is-author-date",
            ],
            quiet=not verbose,
        )
    else:
        if not e.has_head():
            error("Stack entry has no head branch")
            raise RuntimeError

        run_shell_command(["git", "checkout", e.head], quiet=not verbose)

    commit_msg = e.commit.commit_msg()
    found_metadata = RE_STACK_INFO_LINE.search(commit_msg)
    if found_metadata:
        # Metadata is already there, skip this commit
        return needs_rebase

    # Add the stack info metadata to the commit message
    commit_msg += f"\n\nstack-info: PR: {e.pr}, branch: {e.head}"
    run_shell_command(
        ["git", "commit", "--amend", "-F", "-"],
        input=commit_msg.encode(),
        quiet=not verbose,
    )
    return True


def fix_branch_name_template(branch_name_template: str) -> str:
    if "$ID" not in branch_name_template:
        return f"{branch_name_template}/$ID"

    return branch_name_template


@cache
def get_branch_name_base(branch_name_template: str) -> str:
    username = get_gh_username()
    current_branch_name = get_current_branch_name()
    branch_name_base = branch_name_template.replace("$USERNAME", username)
    return branch_name_base.replace("$BRANCH", current_branch_name)


def get_branch_id(branch_name_template: str, branch_name: str) -> str | None:
    branch_name_base = get_branch_name_base(branch_name_template)
    pattern = branch_name_base.replace(r"$ID", r"(\d+)")
    match = re.search(pattern, branch_name)
    if match:
        return match.group(1)
    return None


def generate_branch_name(branch_name_template: str, branch_id: int) -> str:
    branch_name_base = get_branch_name_base(branch_name_template)
    return branch_name_base.replace(r"$ID", str(branch_id))


def get_taken_branch_ids(refs: list[str], branch_name_template: str) -> list[int]:
    branch_ids = [get_branch_id(branch_name_template, ref) for ref in refs]
    return [int(branch_id) for branch_id in branch_ids if branch_id is not None]


def generate_available_branch_name(refs: list[str], branch_name_template: str) -> str:
    branch_ids = get_taken_branch_ids(refs, branch_name_template)
    max_ref_num = max(branch_ids) if branch_ids else 0
    new_branch_id = max_ref_num + 1
    return generate_branch_name(branch_name_template, new_branch_id)


def get_available_branch_name(remote: str, branch_name_template: str) -> str:
    branch_name_base = get_branch_name_base(branch_name_template)

    git_command_branch_template = branch_name_base.replace(r"$ID", "*")
    refs = get_command_output(
        [
            "git",
            "for-each-ref",
            f"refs/remotes/{remote}/{git_command_branch_template}",
            "--format='%(refname)'",
        ]
    ).split()

    refs = [ref.strip("'") for ref in refs]
    return generate_available_branch_name(refs, branch_name_template)


def get_next_available_branch_name(branch_name_template: str, name: str) -> str:
    branch_id = get_branch_id(branch_name_template, name)
    return generate_branch_name(branch_name_template, int(branch_id or 0) + 1)


def set_head_branches(
    st: list[StackEntry], remote: str, *, verbose: bool, branch_name_template: str
) -> None:
    """Set the head ref for each stack entry if it doesn't already have one."""

    run_shell_command(["git", "fetch", "--prune", remote, f"+refs/heads/*:refs/remotes/{remote}/*"], quiet=not verbose)
    available_name = get_available_branch_name(remote, branch_name_template)
    for e in filter(lambda e: not e.has_head(), st):
        e.head = available_name
        available_name = get_next_available_branch_name(
            branch_name_template, available_name
        )


def init_local_branches(
    st: list[StackEntry], remote: str, *, verbose: bool, branch_name_template: str
) -> None:
    log(h("Initializing local branches"))
    set_head_branches(
        st, remote, verbose=verbose, branch_name_template=branch_name_template
    )
    for e in st:
        run_shell_command(
            ["git", "checkout", e.commit.commit_id(), "-B", e.head],
            quiet=not verbose,
        )


def push_branches(st: list[StackEntry], remote: str, *, verbose: bool) -> None:
    log(h("Updating remote branches"))
    cmd = ["git", "push", "-f", remote]
    cmd.extend([f"{e.head}:{e.head}" for e in st])
    run_shell_command(cmd, quiet=not verbose)


def print_cmd_failure_details(exc: SubprocessError) -> None:
    # Test if SubprocessError subclass has stdout and stderr attributes
    if hasattr(exc, "stdout") and exc.stdout:
        cmd_stdout = (
            exc.stdout.decode("utf-8").replace("\\n", "\n").replace("\\t", "\t")
        )
    else:
        cmd_stdout = None

    if hasattr(exc, "stderr") and exc.stderr:
        cmd_stderr = (
            exc.stderr.decode("utf-8").replace("\\n", "\n").replace("\\t", "\t")
        )
    else:
        cmd_stderr = None

    print(f"Exitcode: {exc.returncode if hasattr(exc, 'returncode') else 'unknown'}")
    print(f"Stdout: {cmd_stdout}")
    print(f"Stderr: {cmd_stderr}")


def create_pr(e: StackEntry, *, is_draft: bool, reviewer: str = "") -> None:
    # Don't do anything if the PR already exists
    if e.has_pr():
        return
    if not e.has_base() or not e.has_head():
        error("Stack entry has no base or head branch")
        raise RuntimeError
    log(h("Creating PR " + green(f"'{e.head}' -> '{e.base}'")), level=1)
    cmd = [
        "gh",
        "pr",
        "create",
        "-B",
        e.base or "",
        "-H",
        e.head or "",
        "-t",
        e.commit.title(),
        "-F",
        "-",
    ]
    if reviewer:
        cmd.extend(["--reviewer", reviewer])
    if is_draft:
        cmd.append("--draft")

    try:
        r = get_command_output(cmd, input=e.commit.commit_msg().encode())
    except Exception:
        error(ERROR_CANT_CREATE_PR.format(**locals()))
        raise

    log(b("Created: ") + r, level=2)
    e.pr = r.split()[-1]


def generate_toc(st: list[StackEntry], current: str) -> str:
    def toc_entry(se: StackEntry) -> str:
        pr_id = last(se.pr)
        arrow = "__->__" if pr_id == current else ""
        return f" * {arrow}#{pr_id}\n"

    entries = (toc_entry(se) for se in st[::-1])
    return f"Stacked PRs:\n{''.join(entries)}\n"


def get_current_pr_body(e: StackEntry) -> str:
    out = get_command_output(
        ["gh", "pr", "view", e.pr, "--json", "body"],
    )
    return str(json.loads(out)["body"] or "").strip()


def add_cross_links(st: list[StackEntry], *, keep_body: bool, verbose: bool) -> None:
    for e in st:
        pr_id = last(e.pr)
        pr_toc = generate_toc(st, pr_id)

        title = e.commit.title()
        body = e.commit.commit_msg()

        # Strip title from the body - we will print it separately.
        body = "\n".join(body.splitlines()[1:])

        # Strip stack-info from the body, nothing interesting there.
        body = RE_STACK_INFO_LINE.sub("", body)
        pr_body = [
            f"{pr_toc}",
            f"{CROSS_LINKS_DELIMETER}\n",
        ]

        if keep_body:
            # Keep current body of the PR after the cross links component
            current_pr_body = get_current_pr_body(e)
            pr_body.append(current_pr_body.split(CROSS_LINKS_DELIMETER, 1)[-1].lstrip())
        else:
            pr_body.extend(
                [
                    f"### {title}",
                    "",
                    f"{body}",
                ]
            )

        if e.has_base():
            run_shell_command(
                ["gh", "pr", "edit", e.pr, "-t", title, "-F", "-", "-B", e.base or ""],
                input="\n".join(pr_body).encode(),
                quiet=not verbose,
            )
        else:
            error("Stack entry has no base branch")
            raise RuntimeError


# Temporarily set base branches of existing PRs to the bottom of the stack.
# This needs to be done to avoid PRs getting closed when commits are
# rearranged.
#
# For instance, if we first had
#
# Stack:
#    * #2 (stack/2 -> stack/1)  aaaa
#    * #1 (stack/1 -> main)     bbbb
#
# And then swapped the order of the commits locally and tried submitting again
# we would have:
#
# Stack:
#    * #1 (stack/1 -> main)     bbbb
#    * #2 (stack/2 -> stack/1)  aaaa
#
# Now we need to 1) change bases of the PRs, 2) push branches stack/1 and
# stack/2. If we push stack/1, then PR #2 gets automatically closed, since its
# head branch will contain all the commits from its base branch.
#
# To avoid this, we temporarily set all base branches to point to 'main' - once
# all the branches are pushed we can set the actual base branches.
def reset_remote_base_branches(
    st: list[StackEntry], target: str, *, verbose: bool
) -> None:
    log(h("Resetting remote base branches"), level=1)

    for e in filter(lambda e: e.has_pr(), st):
        run_shell_command(["gh", "pr", "edit", e.pr, "-B", target], quiet=not verbose)


# If local 'main' lags behind 'origin/main', and 'head' contains all commits
# from 'main' to 'origin/main', then we can just move 'main' forward.
#
# It is a common user mistake to not update their local branch, run 'submit',
# and end up with a huge stack of changes that are already merged.
# We could've told users to update their local branch in that scenario, but why
# not to do it for them?
# In the very unlikely case when they indeed wanted to include changes that are
# already in remote into their stack, they can use a different notation for the
# base (e.g. explicit hash of the commit) - but most probably nobody ever would
# need that.
def should_update_local_base(
    head: str, base: str, remote: str, target: str, *, verbose: bool
) -> bool:
    base_hash = get_command_output(["git", "rev-parse", base])
    target_hash = get_command_output(["git", "rev-parse", f"{remote}/{target}"])
    return (
        is_ancestor(base, f"{remote}/{target}", verbose=verbose)
        and is_ancestor(f"{remote}/{target}", head, verbose=verbose)
        and base_hash != target_hash
    )


def update_local_base(base: str, remote: str, target: str, *, verbose: bool) -> None:
    log(h(f"Updating local branch {base} to {remote}/{target}"), level=1)
    run_shell_command(["git", "rebase", f"{remote}/{target}", base], quiet=not verbose)


@dataclass
class CommonArgs:
    """Class to help type checkers and separate implementation for CLI args."""

    base: str
    head: str
    remote: str
    target: str
    hyperlinks: bool
    verbose: bool
    branch_name_template: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> CommonArgs:
        return cls(
            args.base,
            args.head,
            args.remote,
            args.target,
            args.hyperlinks,
            args.verbose,
            args.branch_name_template,
        )


def deduce_base(args: CommonArgs) -> CommonArgs:
    """Deduce the base branch from the head and target branches.

    If the base isn't explicitly specified, find the merge base between
    'origin/main' and 'head'.

    E.g. in the example below we want to include commits E and F into the stack,
    and to do that we pick B as our base:

    --> a ----> b  ----> c ----> d
    (main)       \\         (origin/main)
                  \\
                    ---> e ----> f
                            (head)
   """
    if args.base:
        return args
    deduced_base = get_command_output(
        ["git", "merge-base", args.head, f"{args.remote}/{args.target}"]
    )
    return CommonArgs(
        deduced_base,
        args.head,
        args.remote,
        args.target,
        args.hyperlinks,
        args.verbose,
        args.branch_name_template,
    )


def print_tips_after_export(st: list[StackEntry], args: CommonArgs) -> None:
    stack_size = len(st)
    if stack_size == 0:
        return

    top_commit = args.head
    if top_commit == "HEAD":
        top_commit = get_current_branch_name()

    log(b("\nOnce the stack is reviewed, it is ready to land!"), level=1)
    log(LAND_STACK_TIP.format(**locals()))


# ===----------------------------------------------------------------------=== #
# Entry point for 'submit' command
# ===----------------------------------------------------------------------=== #
def command_submit(
    args: CommonArgs,
    *,
    draft: bool,
    reviewer: str,
    keep_body: bool,
    draft_bitmask: list[bool] | None = None,
) -> None:
    """Entry point for 'submit' command.

    Args:
        args: CommonArgs object containing command line arguments.
        draft: Boolean flag indicating if the PRs should be created as drafts.
        reviewer: String representing the reviewer of the PRs.
        keep_body: Boolean flag indicating if the body of the PRs should be kept.
        draft_bitmask: List of boolean values indicating if each PR should be created as
            a draft.
    """
    log(h("SUBMIT"), level=1)
    current_branch = get_current_branch_name()

    if should_update_local_base(
        head=args.head,
        base=args.base,
        remote=args.remote,
        target=args.target,
        verbose=args.verbose,
    ):
        update_local_base(
            base=args.base, remote=args.remote, target=args.target, verbose=args.verbose
        )
        run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    # Determine what commits belong to the stack
    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    if not st:
        log(h("Empty stack!"))
        log(h(blue("SUCCESS!")))
        return

    if (draft_bitmask is not None) and (len(draft_bitmask) != len(st)):
        log(h("Draft bitmask passed to 'submit' doesn't match number of PRs!"))
        return

    # Create local branches and initialize base and head fields in the stack
    # elements
    init_local_branches(
        st,
        args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    set_base_branches(st, args.target)
    print_stack(st, links=args.hyperlinks)

    # If the current branch contains commits from the stack, we will need to
    # rebase it in the end since the commits will be modified.
    top_branch = st[-1].head
    need_to_rebase_current = is_ancestor(
        top_branch, current_branch, verbose=args.verbose
    )

    reset_remote_base_branches(st, target=args.target, verbose=args.verbose)

    # Push local branches to remote
    push_branches(st, remote=args.remote, verbose=args.verbose)

    # Now we have all the branches, so we can create the corresponding PRs
    log(h("Submitting PRs"), level=1)
    for e_idx, e in enumerate(st):
        is_pr_draft = draft or ((draft_bitmask is not None) and draft_bitmask[e_idx])
        create_pr(e, is_draft=is_pr_draft, reviewer=reviewer)

    # Verify consistency in everything we have so far
    verify(st)

    # Embed stack-info into commit messages
    log(h("Updating commit messages with stack metadata"), level=1)
    needs_rebase = False
    for e in st:
        try:
            needs_rebase = add_or_update_metadata(
                e, needs_rebase=needs_rebase, verbose=args.verbose
            )
        except Exception:
            error(ERROR_CANT_UPDATE_META.format(**locals()))
            raise

    push_branches(st, remote=args.remote, verbose=args.verbose)

    log(h("Adding cross-links to PRs"), level=1)
    add_cross_links(st, keep_body=keep_body, verbose=args.verbose)

    if need_to_rebase_current:
        log(h(f"Rebasing the original branch '{current_branch}'"), level=1)
        run_shell_command(
            [
                "git",
                "rebase",
                top_branch,
                current_branch,
                "--committer-date-is-author-date",
            ],
            quiet=not args.verbose,
        )
    else:
        log(h(f"Checking out the original branch '{current_branch}'"), level=1)
        run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    delete_local_branches(st, verbose=args.verbose)
    print_tips_after_export(st, args)
    log(h(blue("SUCCESS!")), level=1)


# ===----------------------------------------------------------------------=== #
# LAND
# ===----------------------------------------------------------------------=== #
def rebase_pr(e: StackEntry, remote: str, target: str, *, verbose: bool) -> None:
    log(b("Rebasing ") + e.pprint(links=False), level=2)
    # Rebase the head branch to the most recent 'origin/main'
    run_shell_command(["git", "fetch", "--prune", remote], quiet=not verbose)
    cmd = ["git", "checkout", f"{remote}/{e.head}", "-B", e.head]
    try:
        run_shell_command(cmd, quiet=not verbose)
    except Exception:
        error(ERROR_CANT_CHECKOUT_REMOTE_BRANCH.format(**locals()))
        raise

    cmd = [
        "git",
        "rebase",
        f"{remote}/{target}",
        e.head,
        "--committer-date-is-author-date",
    ]
    try:
        run_shell_command(cmd, quiet=not verbose)
    except Exception:
        error(ERROR_CANT_REBASE.format(**locals()))
        raise
    run_shell_command(
        ["git", "push", remote, "-f", f"{e.head}:{e.head}"], quiet=not verbose
    )


def land_pr(e: StackEntry, remote: str, target: str, *, verbose: bool) -> None:
    log(b("Landing ") + e.pprint(links=False), level=2)
    # Rebase the head branch to the most recent 'origin/main'
    run_shell_command(["git", "fetch", "--prune", remote], quiet=not verbose)
    cmd = ["git", "checkout", f"{remote}/{e.head}", "-B", e.head]
    try:
        run_shell_command(cmd, quiet=not verbose)
    except Exception:
        error(ERROR_CANT_CHECKOUT_REMOTE_BRANCH.format(**locals()))
        raise

    # Switch PR base branch to 'main'
    run_shell_command(["gh", "pr", "edit", e.pr, "-B", target], quiet=not verbose)

    # Form the commit message: it should contain the original commit message
    # and nothing else.
    pr_body = RE_STACK_INFO_LINE.sub("", e.commit.commit_msg())

    # Since title is passed separately, we need to strip the first line from the
    # body:
    lines = pr_body.splitlines()
    pr_id = last(e.pr)
    title = f"{lines[0]} (#{pr_id})"
    pr_body = "\n".join(lines[1:]) or " "
    run_shell_command(
        ["gh", "pr", "merge", e.pr, "--squash", "-t", title, "-F", "-"],
        input=pr_body.encode(),
        quiet=not verbose,
    )


def delete_local_branches(st: list[StackEntry], *, verbose: bool) -> None:
    log(h("Deleting local branches"), level=1)
    # Delete local branches
    cmd = ["git", "branch", "-D"]
    cmd.extend([e.head for e in st if e.head])
    run_shell_command(cmd, check=False, quiet=not verbose)


def delete_remote_branches(
    st: list[StackEntry], remote: str, *, verbose: bool, branch_name_template: str
) -> None:
    log(h("Deleting remote branches"), level=1)
    run_shell_command(["git", "fetch", "--prune", remote], quiet=not verbose)

    branch_name_base = get_branch_name_base(branch_name_template)
    refs = get_command_output(
        [
            "git",
            "for-each-ref",
            f"refs/remotes/{remote}/{branch_name_base}",
            "--format=%(refname)",
        ]
    ).split()
    refs = [x.replace(f"refs/remotes/{remote}/", "") for x in refs]
    remote_branches_to_delete = [e.head for e in st if e.head in refs]

    if remote_branches_to_delete:
        cmd = ["git", "push", "-f", remote]
        cmd.extend([f":{branch}" for branch in remote_branches_to_delete])
        run_shell_command(cmd, check=False, quiet=not verbose)


# ===----------------------------------------------------------------------=== #
# Entry point for 'land' command
# ===----------------------------------------------------------------------=== #
def command_land(args: CommonArgs) -> None:
    log(h("LAND"), level=1)

    current_branch = get_current_branch_name()

    if should_update_local_base(
        head=args.head,
        base=args.base,
        remote=args.remote,
        target=args.target,
        verbose=args.verbose,
    ):
        update_local_base(
            base=args.base, remote=args.remote, target=args.target, verbose=args.verbose
        )
        run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    # Determine what commits belong to the stack
    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    if not st:
        log(h("Empty stack!"), level=1)
        log(h(blue("SUCCESS!")), level=1)
        return

    # Initialize base branches of elements in the stack. Head branches should
    # already be there from the metadata that commits need to have by that
    # point.
    set_base_branches(st, args.target)
    print_stack(st, links=args.hyperlinks)

    # Verify that the stack is correct before trying to land it.
    verify(st, check_base=True)

    # All good, land the bottommost PR!
    land_pr(st[0], remote=args.remote, target=args.target, verbose=args.verbose)

    # The rest of the stack now needs to be rebased.
    if len(st) > 1:
        log(h("Rebasing the rest of the stack"), level=1)
        prs_to_rebase = st[1:]
        print_stack(prs_to_rebase, links=args.hyperlinks, level=1)
        for e in prs_to_rebase:
            rebase_pr(e, remote=args.remote, target=args.target, verbose=args.verbose)
        # Change the target of the new bottom-most PR in the stack to 'target'
        run_shell_command(
            ["gh", "pr", "edit", prs_to_rebase[0].pr, "-B", args.target],
            quiet=not args.verbose,
        )

    # Delete local and remote stack branches
    run_shell_command(["git", "checkout", current_branch], quiet=not args.verbose)

    delete_local_branches(st, verbose=args.verbose)

    # If local branch {target} exists, rebase it on the remote/target
    if branch_exists(args.target):
        run_shell_command(
            ["git", "rebase", f"{args.remote}/{args.target}", args.target],
            quiet=not args.verbose,
        )
    run_shell_command(
        ["git", "rebase", f"{args.remote}/{args.target}", current_branch],
        quiet=not args.verbose,
    )

    log(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# ABANDON
# ===----------------------------------------------------------------------=== #
def strip_metadata(e: StackEntry, *, needs_rebase: bool, verbose: bool) -> str:
    """Strip the stack metadata from the commit message and amend the commit.

    Args:
        e: StackEntry object representing the commit to strip metadata from.
        needs_rebase: Boolean flag indicating if the commit needs to be rebased.
        verbose: Boolean flag indicating if verbose output should be printed.

    Returns:
        The SHA of the commit after stripping the metadata.
    """
    m = e.commit.commit_msg()

    m = RE_STACK_INFO_LINE.sub("", m)
    if needs_rebase:
        if not e.has_base() or not e.has_head():
            error("Stack entry has no base or head branch")
            raise RuntimeError
        run_shell_command(
            [
                "git",
                "rebase",
                e.base or "",
                e.head or "",
                "--committer-date-is-author-date",
            ],
            quiet=not verbose,
        )
    else:
        if not e.has_head():
            error("Stack entry has no head branch")
            raise RuntimeError
        run_shell_command(["git", "checkout", e.head or ""], quiet=not verbose)

    run_shell_command(
        ["git", "commit", "--amend", "-F", "-"],
        input=m.encode(),
        quiet=not verbose,
    )

    return get_command_output(["git", "rev-parse", e.head])


# ===----------------------------------------------------------------------=== #
# Entry point for 'abandon' command
# ===----------------------------------------------------------------------=== #
def command_abandon(args: CommonArgs) -> None:
    log(h("ABANDON"))
    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)
    if not st:
        log(h("Empty stack!"))
        log(h(blue("SUCCESS!")))
        return
    current_branch = get_current_branch_name()

    init_local_branches(
        st,
        remote=args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    set_base_branches(st, args.target)
    print_stack(st, links=args.hyperlinks)

    log(h("Stripping stack metadata from commit messages"))

    last_hash = ""
    # The first commit doesn't need to be rebased since its will not change.
    # The rest of the commits need to be rebased since their base will be
    # changed as we strip the metadata from the commit messages.
    need_rebase = False
    for e in st:
        last_hash = strip_metadata(e, needs_rebase=need_rebase, verbose=args.verbose)
        need_rebase = True

    log(h("Rebasing the current branch on top of updated top branch"))
    run_shell_command(
        ["git", "rebase", last_hash, current_branch], quiet=not args.verbose
    )

    delete_local_branches(st, verbose=args.verbose)
    delete_remote_branches(
        st,
        remote=args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    log(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# VIEW
# ===----------------------------------------------------------------------=== #
def print_tips_after_view(st: list[StackEntry], args: CommonArgs) -> None:
    stack_size = len(st)
    if stack_size == 0:
        return

    ready_to_land = all(not e.has_missing_info() for e in st)

    top_commit = args.head
    if top_commit == "HEAD":
        top_commit = get_current_branch_name()

    if ready_to_land:
        log(b("\nThis stack is ready to land!"))
        log(UPDATE_STACK_TIP.format(**locals()))
        log(LAND_STACK_TIP.format(**locals()))
        return

    # Stack is not ready to land, suggest exporting it first
    log(b("\nThis stack can't be landed yet, you need to export it first."))
    log(EXPORT_STACK_TIP.format(**locals()))


# ===----------------------------------------------------------------------=== #
# Entry point for 'view' command
# ===----------------------------------------------------------------------=== #
def command_view(args: CommonArgs) -> None:
    log(h("VIEW"))

    if should_update_local_base(
        head=args.head,
        base=args.base,
        remote=args.remote,
        target=args.target,
        verbose=args.verbose,
    ):
        log(
            red(
                f"\nWarning: Local '{args.base}' is behind"
                f" '{args.remote}/{args.target}'!"
            ),
        )
        log(
            ("Consider updating your local branch by running the following commands:"),
        )
        log(
            b(f"   git rebase {args.remote}/{args.target} {args.base}"),
        )
        log(
            b(f"   git checkout {get_current_branch_name()}\n"),
        )

    st = get_stack(base=args.base, head=args.head, verbose=args.verbose)

    set_head_branches(
        st,
        remote=args.remote,
        verbose=args.verbose,
        branch_name_template=args.branch_name_template,
    )
    set_base_branches(st, target=args.target)
    print_stack(st, links=args.hyperlinks)
    print_tips_after_view(st, args)
    log(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# Main entry point
# ===----------------------------------------------------------------------=== #


def create_argparser(
    config: configparser.ConfigParser,
) -> argparse.ArgumentParser:
    """Helper for CL option definition and parsing logic."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(help="sub-command help", dest="command")

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "-R",
        "--remote",
        default=config.get("repo", "remote", fallback="origin"),
        help="Remote name",
    )
    common_parser.add_argument("-B", "--base", help="Local base branch")
    common_parser.add_argument("-H", "--head", default="HEAD", help="Local head branch")
    common_parser.add_argument(
        "-T",
        "--target",
        default=config.get("repo", "target", fallback="main"),
        help="Remote target branch",
    )
    common_parser.add_argument(
        "--hyperlinks",
        action=argparse.BooleanOptionalAction,
        default=config.getboolean("common", "hyperlinks", fallback=True),
        help="Enable or disable hyperlink support.",
    )
    common_parser.add_argument(
        "-V",
        "--verbose",
        action="store_true",
        default=config.getboolean("common", "verbose", fallback=False),
        help="Enable verbose output from Git subcommands.",
    )
    common_parser.add_argument(
        "--branch-name-template",
        default=config.get("repo", "branch_name_template", fallback="$USERNAME/stack"),
        help="A template for names of the branches stack-pr would use.",
    )

    parser_submit = subparsers.add_parser(
        "submit",
        aliases=["export"],
        help="Submit a stack of PRs",
        parents=[common_parser],
    )
    parser_submit.add_argument(
        "--keep-body",
        action="store_true",
        default=config.getboolean("common", "keep_body", fallback=False),
        help="Keep current PR body and only add/update cross links",
    )
    parser_submit.add_argument(
        "-d",
        "--draft",
        action="store_true",
        default=config.getboolean("common", "draft", fallback=False),
        help="Submit PRs in draft mode",
    )
    parser_submit.add_argument(
        "--draft-bitmask",
        type=draft_bitmask_type,
        default=None,
        help="Bitmask of whether each PR is a draft (optional).",
    )
    parser_submit.add_argument(
        "--reviewer",
        default=os.getenv(
            "STACK_PR_DEFAULT_REVIEWER",
            default=config.get("repo", "reviewer", fallback=""),
        ),
        help="List of reviewers for the PR",
    )
    parser_submit.add_argument(
        "-s",
        "--stash",
        action="store_true",
        default=config.getboolean("common", "stash", fallback=False),
        help="Stash all uncommited changes before submitting the PR",
    )

    subparsers.add_parser(
        "land",
        help="Land the current stack",
        parents=[common_parser],
    )
    subparsers.add_parser(
        "abandon",
        help="Abandon the current stack",
        parents=[common_parser],
    )
    subparsers.add_parser(
        "view",
        help="Inspect the current stack",
        parents=[common_parser],
    )

    return parser


def load_config(config_file: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if Path(config_file).is_file():
        config.read(config_file)
    return config


def main() -> None:  # noqa: PLR0912
    config_file = os.getenv("STACKPR_CONFIG", ".stack-pr.cfg")
    config = load_config(config_file)

    parser = create_argparser(config)
    args = parser.parse_args()

    if not args.command:
        print(h(red("Invalid usage of the stack-pr command.")))
        parser.print_help()
        return

    # Make sure "$ID" is present in the branch name template and append it if not
    args.branch_name_template = fix_branch_name_template(args.branch_name_template)
    common_args = CommonArgs.from_args(args)

    if common_args.verbose:
        logger.setLevel(logging.DEBUG)

    check_gh_installed()

    current_branch = get_current_branch_name()
    get_branch_name_base(common_args.branch_name_template)
    try:
        if args.command in ["submit", "export"] and args.stash:
            run_shell_command(["git", "stash", "save"], quiet=not common_args.verbose)

        if args.command != "view" and not is_repo_clean():
            error(ERROR_REPO_DIRTY)
            return
        common_args = deduce_base(common_args)

        if args.command in ["submit", "export"]:
            command_submit(
                common_args,
                draft=args.draft,
                reviewer=args.reviewer,
                keep_body=args.keep_body,
                draft_bitmask=args.draft_bitmask,
            )
        elif args.command == "land":
            command_land(common_args)
        elif args.command == "abandon":
            command_abandon(common_args)
        elif args.command == "view":
            command_view(common_args)
        else:
            print(h(red("Unknown command: " + args.command)))
            return
    except Exception as exc:
        # If something failed, checkout the original branch
        run_shell_command(
            ["git", "checkout", current_branch], quiet=not common_args.verbose
        )
        if isinstance(exc, SubprocessError):
            print_cmd_failure_details(exc)
        raise
    finally:
        if args.command in ["submit", "export"] and args.stash:
            run_shell_command(["git", "stash", "pop"], quiet=not common_args.verbose)


if __name__ == "__main__":
    main()
