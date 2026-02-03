"""Tests for MCP server tools."""

import pytest


def test_server_imports():
    """Test that the server module can be imported without errors."""
    from ciwatch_mcp import server

    assert server.mcp is not None


def test_analyze_main_branch_signature():
    """Test that analyze_main_branch has correct signature."""
    from ciwatch_mcp.server import analyze_main_branch
    import inspect

    sig = inspect.signature(analyze_main_branch)
    params = list(sig.parameters.keys())

    # Verify all expected parameters exist
    expected_params = [
        "pipeline",
        "branch",
        "repo",
        "search_github",
        "detail_level",
        "max_builds",
        "max_failures",
        "hours_lookback",
        "exclude_scheduled",
    ]

    for param in expected_params:
        assert param in params, f"Missing parameter: {param}"


def test_new_models_importable():
    """Test that new models can be imported."""
    from ciwatch_mcp.models import (
        FailureClassificationWithRecurrence,
        MainBranchAnalysisResult,
    )

    assert FailureClassificationWithRecurrence is not None
    assert MainBranchAnalysisResult is not None


def test_new_config_constants():
    """Test that new config constants exist."""
    from ciwatch_mcp.config import (
        DEFAULT_MAIN_ANALYSIS_BUILDS,
        DEFAULT_MAIN_ANALYSIS_HOURS,
        DEFAULT_PERSISTENT_THRESHOLD,
    )

    assert DEFAULT_MAIN_ANALYSIS_HOURS == 24
    assert DEFAULT_MAIN_ANALYSIS_BUILDS == 5
    assert DEFAULT_PERSISTENT_THRESHOLD == 0.5
