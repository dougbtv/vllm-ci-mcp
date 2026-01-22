"""Tests for test history assessment logic."""

from ciwatch_mcp.assessment import (
    assess_test_history,
    calculate_fail_rate,
    consistent_fingerprint_after,
    find_transition_point,
    generate_summary,
)


def test_calculate_fail_rate_all_passed():
    """Test fail rate calculation when all tests passed."""
    timeline = [
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "pass"},
    ]
    assert calculate_fail_rate(timeline) == 0.0


def test_calculate_fail_rate_all_failed():
    """Test fail rate calculation when all tests failed."""
    timeline = [
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "fail"},
    ]
    assert calculate_fail_rate(timeline) == 1.0


def test_calculate_fail_rate_mixed():
    """Test fail rate calculation with mixed outcomes."""
    timeline = [
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "fail"},
    ]
    assert calculate_fail_rate(timeline) == 0.5


def test_calculate_fail_rate_window():
    """Test fail rate calculation within a window."""
    timeline = [
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "fail"},
    ]
    # First half should be 0% fail rate
    assert calculate_fail_rate(timeline, 0, 2) == 0.0
    # Second half should be 100% fail rate
    assert calculate_fail_rate(timeline, 2, 4) == 1.0


def test_find_transition_point_clear_transition():
    """Test finding clear pass→fail transition."""
    timeline = [
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "fail"},
    ]
    transition_idx = find_transition_point(timeline)
    assert transition_idx == 2  # Transition at index 2


def test_find_transition_point_no_transition():
    """Test that no transition is found when pattern is consistent."""
    timeline = [
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "fail"},
        {"test_found": True, "test_status": "fail"},
    ]
    transition_idx = find_transition_point(timeline)
    assert transition_idx is None


def test_find_transition_point_insufficient_data():
    """Test that None is returned with insufficient data."""
    timeline = [
        {"test_found": True, "test_status": "pass"},
        {"test_found": True, "test_status": "fail"},
    ]
    transition_idx = find_transition_point(timeline)
    assert transition_idx is None  # Less than 3 entries


def test_consistent_fingerprint_after_true():
    """Test detecting consistent fingerprint after transition."""
    timeline = [
        {"test_status": "pass", "jobs": []},
        {"test_status": "pass", "jobs": []},
        {
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
        {
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
        {
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
    ]
    assert consistent_fingerprint_after(timeline, 2) is True


def test_consistent_fingerprint_after_false():
    """Test detecting inconsistent fingerprints after transition."""
    timeline = [
        {"test_status": "pass", "jobs": []},
        {"test_status": "pass", "jobs": []},
        {
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
        {
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error B"}],
        },
        {
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error C"}],
        },
    ]
    assert consistent_fingerprint_after(timeline, 2) is False


def test_assess_regression():
    """Test regression detection with clear transition."""
    timeline = [
        {
            "build_number": 1,
            "build_url": "http://build/1",
            "commit_sha": "abc123",
            "test_found": True,
            "test_status": "pass",
            "jobs": [],
        },
        {
            "build_number": 2,
            "build_url": "http://build/2",
            "commit_sha": "def456",
            "test_found": True,
            "test_status": "pass",
            "jobs": [],
        },
        {
            "build_number": 3,
            "build_url": "http://build/3",
            "commit_sha": "ghi789",
            "test_found": True,
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
        {
            "build_number": 4,
            "build_url": "http://build/4",
            "commit_sha": "jkl012",
            "test_found": True,
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
        {
            "build_number": 5,
            "build_url": "http://build/5",
            "commit_sha": "mno345",
            "test_found": True,
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
    ]
    result = assess_test_history(timeline)
    assert result["classification"] == "REGRESSION"
    assert result["confidence"] == "HIGH"
    assert result["transition_build"] == 3


def test_assess_flake_onset():
    """Test flake detection with alternating outcomes."""
    timeline = [
        {
            "build_number": i,
            "test_found": True,
            "test_status": "pass" if i % 2 == 0 else "fail",
            "jobs": [{"fingerprint_normalized": f"Error {i % 3}"}] if i % 2 == 1 else [],
        }
        for i in range(10)
    ]
    result = assess_test_history(timeline)
    assert result["classification"] in ["FLAKE_ONSET", "SPORADIC"]


def test_assess_persistent_fail():
    """Test persistent failure detection."""
    timeline = [
        {
            "build_number": i,
            "test_found": True,
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        }
        for i in range(10)
    ]
    result = assess_test_history(timeline)
    assert result["classification"] == "PERSISTENT_FAIL"
    assert result["confidence"] == "HIGH"


def test_assess_sporadic():
    """Test sporadic failure detection (rare failures)."""
    timeline = [
        {
            "build_number": i,
            "test_found": True,
            "test_status": "fail" if i == 5 else "pass",
            "jobs": [{"fingerprint_normalized": "Error A"}] if i == 5 else [],
        }
        for i in range(20)
    ]
    result = assess_test_history(timeline)
    assert result["classification"] == "SPORADIC"
    assert result["confidence"] == "HIGH"


def test_assess_insufficient_data():
    """Test insufficient data classification."""
    timeline = [
        {
            "build_number": 1,
            "test_found": True,
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        }
    ]
    result = assess_test_history(timeline)
    assert result["classification"] == "INSUFFICIENT_DATA"
    assert result["confidence"] == "LOW"


def test_generate_summary_basic():
    """Test summary generation with basic timeline."""
    timeline = [
        {
            "build_number": 1,
            "build_url": "http://build/1",
            "commit_sha": "abc123",
            "created_at": "2024-01-22T10:00:00Z",
            "test_found": True,
            "test_status": "pass",
            "jobs": [],
        },
        {
            "build_number": 2,
            "build_url": "http://build/2",
            "commit_sha": "def456",
            "created_at": "2024-01-22T11:00:00Z",
            "test_found": True,
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
    ]
    assessment = {
        "classification": "SPORADIC",
        "confidence": "MED",
        "notes": ["Test is mostly stable"],
    }
    summary = generate_summary("tests/test_foo.py::test_bar", timeline, assessment)

    assert "tests/test_foo.py::test_bar" in summary
    assert "SPORADIC" in summary
    assert "MED" in summary
    assert "Test is mostly stable" in summary
    assert "Passed: 1" in summary
    assert "Failed: 1" in summary


def test_generate_summary_with_regression():
    """Test summary generation with regression info."""
    timeline = [
        {
            "build_number": 1,
            "build_url": "http://build/1",
            "commit_sha": "abc123",
            "created_at": "2024-01-22T10:00:00Z",
            "test_found": True,
            "test_status": "pass",
            "jobs": [],
        },
        {
            "build_number": 2,
            "build_url": "http://build/2",
            "commit_sha": "def456",
            "created_at": "2024-01-22T11:00:00Z",
            "test_found": True,
            "test_status": "fail",
            "jobs": [{"fingerprint_normalized": "Error A"}],
        },
    ]
    assessment = {
        "classification": "REGRESSION",
        "confidence": "HIGH",
        "notes": ["Clear transition detected"],
        "transition_build": 2,
    }
    summary = generate_summary("tests/test_foo.py::test_bar", timeline, assessment)

    assert "REGRESSION" in summary
    assert "Build: [2]" in summary or "2" in summary
    assert "def456" in summary  # Commit SHA should be included
