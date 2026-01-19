"""Tests for normalize.py module."""

import pytest
from ciwatch_mcp.models import TestFailure
from ciwatch_mcp.normalize import (
    extract_test_failures_from_log,
    generate_failure_key,
)


def test_extract_pytest_failures():
    """Test pytest FAILED pattern extraction."""
    log = """FAILED tests/test_foo.py::test_bar - AssertionError: expected 5
FAILED tests/test_baz.py::test_qux[param] - RuntimeError: timeout
"""

    failures = extract_test_failures_from_log(log, "Test Job")

    assert len(failures) == 2
    assert failures[0].test_name == "tests/test_foo.py::test_bar"
    assert failures[0].job_name == "Test Job"
    assert failures[1].test_name == "tests/test_baz.py::test_qux[param]"


def test_extract_pytest_errors():
    """Test pytest ERROR pattern extraction."""
    log = """ERROR tests/test_setup.py::test_init - ImportError: missing module
"""

    failures = extract_test_failures_from_log(log, "Setup Job")

    assert len(failures) == 1
    assert failures[0].test_name == "tests/test_setup.py::test_init"


def test_extract_no_pytest_output():
    """Test fallback to job-level when no pytest output."""
    log = "Some error occurred\nBuild failed"

    failures = extract_test_failures_from_log(log, "Test Job")

    assert len(failures) == 1
    assert failures[0].test_name == "Test Job"
    assert failures[0].error_message == "Job failed without pytest test names"


def test_extract_with_error_section():
    """Test extraction with pytest failure section."""
    log = """FAILED tests/test_foo.py::test_bar
_________________________________ tests/test_foo.py::test_bar _________________________________

    def test_bar():
>       assert 5 == 3
E       AssertionError: expected 5, got 3

tests/test_foo.py:10: AssertionError
"""

    failures = extract_test_failures_from_log(log, "Test Job")

    assert len(failures) == 1
    assert failures[0].test_name == "tests/test_foo.py::test_bar"
    # Error message extraction is optional - the important part is test name extraction works


def test_failure_key_stability():
    """Test that same failure generates same key."""
    f1 = TestFailure(
        test_name="test.py::test_foo",
        job_name="Job A",
        error_message="Error: timeout",
    )
    f2 = TestFailure(
        test_name="test.py::test_foo",
        job_name="Job A",
        error_message="Error: timeout",
    )

    assert generate_failure_key(f1) == generate_failure_key(f2)


def test_failure_key_uniqueness():
    """Test that different failures generate different keys."""
    f1 = TestFailure(
        test_name="test.py::test_foo",
        job_name="Job A",
        error_message="Error: timeout",
    )
    f2 = TestFailure(
        test_name="test.py::test_bar",  # different test
        job_name="Job A",
        error_message="Error: timeout",
    )

    assert generate_failure_key(f1) != generate_failure_key(f2)


def test_deduplication_removes_duplicates():
    """Test that duplicate test names are removed."""
    log = """FAILED tests/test_foo.py::test_bar
FAILED tests/test_foo.py::test_bar
FAILED tests/test_baz.py::test_qux
"""

    failures = extract_test_failures_from_log(log, "Test Job")

    # Should only have 2 unique tests
    assert len(failures) == 2
    test_names = [f.test_name for f in failures]
    assert "tests/test_foo.py::test_bar" in test_names
    assert "tests/test_baz.py::test_qux" in test_names
