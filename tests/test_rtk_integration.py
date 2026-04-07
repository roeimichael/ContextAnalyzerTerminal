"""Tests for the RTK integration module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from context_analyzer_tool.rtk_integration import (
    enhance_suggestion_with_rtk,
    get_rtk_db_path,
    get_rtk_version,
    is_rtk_installed,
    recommend_rtk_install,
)

# ---------------------------------------------------------------------------
# 1. is_rtk_installed
# ---------------------------------------------------------------------------


@patch("context_analyzer_tool.rtk_integration.shutil.which", return_value=None)
def test_is_rtk_installed_not_found(mock_which: MagicMock) -> None:
    """When shutil.which returns None, is_rtk_installed should be False."""
    assert is_rtk_installed() is False
    mock_which.assert_called_once_with("rtk")


@patch("context_analyzer_tool.rtk_integration.shutil.which", return_value="/usr/local/bin/rtk")
def test_is_rtk_installed_found(mock_which: MagicMock) -> None:
    """When shutil.which returns a path, is_rtk_installed should be True."""
    assert is_rtk_installed() is True
    mock_which.assert_called_once_with("rtk")


# ---------------------------------------------------------------------------
# 2. get_rtk_version
# ---------------------------------------------------------------------------


@patch("context_analyzer_tool.rtk_integration.shutil.which", return_value=None)
def test_get_rtk_version_not_installed(mock_which: MagicMock) -> None:
    """When rtk is not on PATH, get_rtk_version should return None."""
    assert get_rtk_version() is None


@patch("context_analyzer_tool.rtk_integration.subprocess.run")
@patch(
    "context_analyzer_tool.rtk_integration.shutil.which",
    return_value="/usr/local/bin/rtk",
)
def test_get_rtk_version_success(
    mock_which: MagicMock,
    mock_run: MagicMock,
) -> None:
    """When rtk --version succeeds, get_rtk_version returns the output."""
    mock_run.return_value = MagicMock(returncode=0, stdout="rtk 0.34.1\n")
    result = get_rtk_version()
    assert result is not None
    assert "0.34.1" in result
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# 3. get_rtk_db_path
# ---------------------------------------------------------------------------


@patch.dict("os.environ", {}, clear=False)
@patch("context_analyzer_tool.rtk_integration.Path.exists", return_value=False)
def test_get_rtk_db_path_not_found(mock_exists: MagicMock) -> None:
    """When no candidate path exists, get_rtk_db_path returns None."""
    # Also ensure RTK_DB_PATH env var is not set
    with patch.dict("os.environ", {"RTK_DB_PATH": ""}, clear=False):
        result = get_rtk_db_path()
    assert result is None


# ---------------------------------------------------------------------------
# 4. enhance_suggestion_with_rtk
# ---------------------------------------------------------------------------


@patch(
    "context_analyzer_tool.rtk_integration.is_rtk_installed",
    return_value=False,
)
def test_enhance_suggestion_with_rtk_not_installed(
    mock_installed: MagicMock,
) -> None:
    """When RTK is not installed and tool is Bash, append install recommendation."""
    result = enhance_suggestion_with_rtk("Bash", "Original suggestion.")
    assert "rtk-py" in result or "RTK" in result
    assert "pip install" in result
    # The original suggestion text should still be present.
    assert "Original suggestion." in result


@patch(
    "context_analyzer_tool.rtk_integration.is_rtk_hooks_installed",
    return_value=False,
)
@patch(
    "context_analyzer_tool.rtk_integration.is_rtk_installed",
    return_value=True,
)
def test_enhance_suggestion_with_rtk_installed(
    mock_installed: MagicMock,
    mock_hooks: MagicMock,
) -> None:
    """When RTK is installed but hooks are not active, suggest hook setup."""
    result = enhance_suggestion_with_rtk("Bash", "Original suggestion.")
    # Should mention hooks / init rather than installation.
    assert "hooks" in result.lower() or "rtk init" in result
    # Should NOT recommend installing rtk-py.
    assert "pip install" not in result


def test_enhance_suggestion_non_bash() -> None:
    """Non-Bash tools should not receive any RTK suggestion."""
    for tool in ("Read", "Write", "Edit", "Glob", "Grep"):
        result = enhance_suggestion_with_rtk(tool, "Some suggestion.")
        assert result == "Some suggestion."
        # Also verify no RTK-related text leaked in.
        assert "RTK" not in result
        assert "rtk" not in result


# ---------------------------------------------------------------------------
# 5. recommend_rtk_install
# ---------------------------------------------------------------------------


def test_recommend_rtk_install() -> None:
    """recommend_rtk_install should return a string mentioning rtk-py."""
    message = recommend_rtk_install()
    assert isinstance(message, str)
    assert "rtk-py" in message
