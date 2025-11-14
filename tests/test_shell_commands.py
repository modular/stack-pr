import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "src"))

import pytest

from stack_pr.shell_commands import run_shell_command


def test_cmd_success_quiet_false_print(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are printed when quiet=False on success."""
    # Use a command that produces both stdout and stderr
    # sh -c 'echo "out" && echo "err" >&2' produces both
    result = run_shell_command(
        ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2'],
        quiet=False,
    )

    # stdout and stderr are not captured in memory
    assert result.returncode == 0
    assert result.stdout is None
    assert result.stderr is None

    # stdout and stderr are printed to console
    captured = capfd.readouterr()
    assert "stdout_msg" in captured.out
    assert "stderr_msg" in captured.err


def test_cmd_success_quiet_true_captured(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are captured when quiet=True on success."""
    result = run_shell_command(
        ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2'],
        quiet=True,
    )

    # stdout and stderr are captured in memory
    assert result.returncode == 0
    assert "stdout_msg" in result.stdout.decode("utf-8")
    assert "stderr_msg" in result.stderr.decode("utf-8")

    # stdout and stderr are not printed to console
    captured = capfd.readouterr()
    assert "stdout_msg" not in captured.out
    assert "stderr_msg" not in captured.err


def test_cmd_fail_quiet_true_captured(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are caught by exception handling.

    Tests behavior when quiet=True on failure.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        run_shell_command(
            ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2 && exit 1'],
            quiet=True,
        )

    # stdout and stderr are captured in exception info
    exception = exc.value
    assert "stdout_msg" in exception.stdout.decode("utf-8")
    assert "stderr_msg" in exception.stderr.decode("utf-8")

    # stdout and stderr are not printed to console
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cmd_fail_quiet_false_print(capfd: pytest.CaptureFixture) -> None:
    """Test that stdout and stderr are printed when quiet=False on failure."""
    with pytest.raises(subprocess.CalledProcessError):
        run_shell_command(
            ["sh", "-c", 'echo "stdout_msg" && echo "stderr_msg" >&2 && exit 1'],
            quiet=False,
        )

    # stdout and stderr are printed to console
    captured = capfd.readouterr()
    assert "stdout_msg" in captured.out
    assert "stderr_msg" in captured.err
