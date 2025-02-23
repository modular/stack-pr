import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

from stack_pr.cli import (
    get_branch_id,
    generate_branch_name,
    get_taken_branch_ids,
    get_gh_username,
    generate_available_branch_name,
)

import pytest


@pytest.fixture(scope="module")
def username():
    return get_gh_username()


@pytest.mark.parametrize(
    "template,branch_name,expected",
    [
        ("feature-$ID-desc", "feature-123-desc", "123"),
        ("$USERNAME/stack/$ID", "{username}/stack/99", "99"),
        ("$USERNAME/stack/$ID", "refs/remote/origin/{username}/stack/99", "99"),
    ],
)
def test_get_branch_id(username, template, branch_name, expected):
    branch_name = branch_name.format(username=username)
    assert get_branch_id(template, branch_name) == expected


@pytest.mark.parametrize(
    "template,branch_name",
    [
        ("feature/$ID/desc", "feature/abc/desc"),
        ("feature/$ID/desc", "wrong/format"),
        ("$USERNAME/stack/$ID", "{username}/main/99"),
    ],
)
def test_get_branch_id_no_match(username, template, branch_name):
    branch_name = branch_name.format(username=username)
    assert get_branch_id(template, branch_name) is None


def test_generate_branch_name():
    template = "feature/$ID/description"
    assert generate_branch_name(template, "123") == "feature/123/description"


def test_get_taken_branch_ids():
    template = "User/stack/$ID"
    refs = [
        "refs/remotes/origin/User/stack/104",
        "refs/remotes/origin/User/stack/105",
        "refs/remotes/origin/User/stack/134",
    ]
    assert get_taken_branch_ids(refs, template) == [104, 105, 134]
    refs = ["User/stack/104", "User/stack/105", "User/stack/134"]
    assert get_taken_branch_ids(refs, template) == [104, 105, 134]
    refs = ["User/stack/104", "AAAA/stack/105", "User/stack/134", "User/stack/bbb"]
    assert get_taken_branch_ids(refs, template) == [104, 134]


def test_generate_available_branch_name():
    template = "User/stack/$ID"
    refs = [
        "refs/remotes/origin/User/stack/104",
        "refs/remotes/origin/User/stack/105",
        "refs/remotes/origin/User/stack/134",
    ]
    assert generate_available_branch_name(refs, template) == "User/stack/135"
    refs = []
    assert generate_available_branch_name(refs, template) == "User/stack/1"
    template = "User-stack-$ID"
    refs = [
        "refs/remotes/origin/User-stack-104",
        "refs/remotes/origin/User-stack-105",
        "refs/remotes/origin/User-stack-134",
    ]
    assert generate_available_branch_name(refs, template) == "User-stack-135"
