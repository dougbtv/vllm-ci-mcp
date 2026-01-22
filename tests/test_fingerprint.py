"""Tests for failure fingerprint normalization."""

from ciwatch_mcp.fingerprint import (
    extract_fingerprint_from_log,
    normalize_failure_fingerprint,
)


def test_normalize_floats():
    """Test that floating point numbers are normalized to <NUM>."""
    input_msg = "accuracy: 0.590 < 0.620"
    expected = "accuracy: <NUM> < <NUM>"
    assert normalize_failure_fingerprint(input_msg) == expected


def test_normalize_integers():
    """Test that integers are normalized to <NUM>."""
    input_msg = "Expected 5 items but got 10"
    expected = "Expected <NUM> items but got <NUM>"
    assert normalize_failure_fingerprint(input_msg) == expected


def test_normalize_memory_addresses():
    """Test that memory addresses are normalized to <ADDR>."""
    input_msg = "Object at 0x7f8a3c failed"
    expected = "Object at <ADDR> failed"
    assert normalize_failure_fingerprint(input_msg) == expected


def test_normalize_uuids():
    """Test that UUIDs are normalized to <UUID>."""
    input_msg = "Request 550e8400-e29b-41d4-a716-446655440000 failed"
    expected = "Request <UUID> failed"
    assert normalize_failure_fingerprint(input_msg) == expected


def test_normalize_timestamps():
    """Test that timestamps are normalized to <TIMESTAMP>."""
    input_msg = "Event at 2024-01-22T10:30:45 was late"
    expected = "Event at <TIMESTAMP> was late"
    assert normalize_failure_fingerprint(input_msg) == expected


def test_normalize_preserves_structure():
    """Test that normalization preserves error message structure."""
    input_msg = "AssertionError: DBO+DP+EP accuracy too low (deepep_low_latency): 0.590 < 0.620"
    expected = "AssertionError: DBO+DP+EP accuracy too low (deepep_low_latency): <NUM> < <NUM>"
    assert normalize_failure_fingerprint(input_msg) == expected


def test_normalize_combined():
    """Test normalization with multiple types of replacements."""
    input_msg = "Test at 0x7fff failed at 2024-01-22 10:30:45 with code 42"
    expected = "Test at <ADDR> failed at <TIMESTAMP> with code <NUM>"
    assert normalize_failure_fingerprint(input_msg) == expected


def test_extract_fingerprint_from_log_with_failed():
    """Test extracting fingerprint from log with FAILED marker."""
    log_text = """
FAILED tests/test_foo.py::test_bar

___________ tests/test_foo.py::test_bar ___________

    def test_bar():
>       assert 0.590 > 0.620
E       AssertionError: accuracy too low: 0.590 < 0.620

tests/test_foo.py:42: AssertionError
"""
    fingerprint = extract_fingerprint_from_log(log_text, "tests/test_foo.py::test_bar")
    assert fingerprint is not None
    assert "<NUM>" in fingerprint  # Should have normalized numbers
    assert "AssertionError" in fingerprint


def test_extract_fingerprint_from_log_no_section():
    """Test extracting fingerprint when no delimited section exists."""
    log_text = """
Running tests...
FAILED tests/test_foo.py::test_bar
RuntimeError: Connection failed
Some other output
"""
    fingerprint = extract_fingerprint_from_log(log_text, "tests/test_foo.py::test_bar")
    assert fingerprint is not None
    assert "RuntimeError" in fingerprint


def test_extract_fingerprint_not_found():
    """Test that None is returned when test not found in log."""
    log_text = """
PASSED tests/other_test.py::test_something
All tests passed
"""
    fingerprint = extract_fingerprint_from_log(log_text, "tests/test_foo.py::test_bar")
    assert fingerprint is None


def test_extract_fingerprint_normalizes_output():
    """Test that extracted fingerprint is normalized."""
    log_text = """
FAILED tests/test_foo.py::test_bar

___________ tests/test_foo.py::test_bar ___________

TimeoutError: Request timed out after 30 seconds at 0x7f8a3c
"""
    fingerprint = extract_fingerprint_from_log(log_text, "tests/test_foo.py::test_bar")
    assert fingerprint is not None
    assert "<NUM>" in fingerprint  # 30 should be normalized
    assert "<ADDR>" in fingerprint  # Memory address should be normalized
    assert "TimeoutError" in fingerprint
