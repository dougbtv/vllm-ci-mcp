"""Failure fingerprint extraction and normalization for deduplication."""

import re
from typing import Optional

# Normalization patterns (applied in order - specific patterns first!)
NORMALIZATION_PATTERNS = [
    # UUIDs: abc-123-def -> <UUID> (must be before integers)
    (re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'), '<UUID>'),
    # Timestamps: 2024-01-22T10:00:00 -> <TIMESTAMP> (must be before integers)
    (re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'), '<TIMESTAMP>'),
    # Memory addresses: 0x7f8a3c -> <ADDR>
    (re.compile(r'0x[0-9a-fA-F]+'), '<ADDR>'),
    # Floats: 0.590 -> <NUM>
    (re.compile(r'\b\d+\.\d+\b'), '<NUM>'),
    # Integers: 123 -> <NUM>
    (re.compile(r'\b\d+\b'), '<NUM>'),
]


def normalize_failure_fingerprint(error_message: str) -> str:
    """Apply normalization patterns to failure message for deduplication.

    Normalizes:
    - Floating point numbers (0.590 -> <NUM>)
    - Integers (123 -> <NUM>)
    - Memory addresses (0x7f8a3c -> <ADDR>)
    - UUIDs
    - Timestamps

    This allows grouping failures with same structure but different values.

    Args:
        error_message: Raw error message from test failure

    Returns:
        Normalized error message with variable parts replaced by placeholders
    """
    normalized = error_message
    for pattern, replacement in NORMALIZATION_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def extract_fingerprint_from_log(log_text: str, test_nodeid: str) -> Optional[str]:
    """Extract and normalize failure fingerprint from log text.

    Looks for the test's failure section and extracts the error message.

    Args:
        log_text: Raw log text
        test_nodeid: Full pytest nodeid (e.g., "tests/test_foo.py::test_bar")

    Returns:
        Normalized fingerprint string, or None if not found
    """
    # Look for FAILED line
    escaped_test = re.escape(test_nodeid)
    failed_match = re.search(rf"^FAILED {escaped_test}", log_text, re.MULTILINE)
    if not failed_match:
        return None

    # Look for test section delimited by underscores
    test_section_match = re.search(
        rf"_{10,}\s+{escaped_test}\s+_{10,}(.*?)(?=_{10,}|\Z)",
        log_text,
        re.DOTALL,
    )

    if not test_section_match:
        # No section found, use simple heuristic
        # Find the FAILED line and grab next few lines
        failed_pos = failed_match.start()
        context = log_text[failed_pos:failed_pos + 500]
        # Look for exception patterns
        error_patterns = [
            re.compile(r"(\w+Error): (.+?)(?:\n|$)"),
            re.compile(r"AssertionError: (.+?)(?:\n|$)"),
            re.compile(r"RuntimeError: (.+?)(?:\n|$)"),
            re.compile(r"TimeoutError: (.+?)(?:\n|$)"),
        ]
        for pattern in error_patterns:
            match = pattern.search(context)
            if match:
                error_msg = match.group(0).strip()
                return normalize_failure_fingerprint(error_msg)
        return None

    # Extract error from section
    section_text = test_section_match.group(1)

    # Try to extract error message
    error_patterns = [
        re.compile(r"(\w+Error): (.+?)(?:\n|$)"),
        re.compile(r"AssertionError: (.+?)(?:\n|$)"),
        re.compile(r"RuntimeError: (.+?)(?:\n|$)"),
        re.compile(r"TimeoutError: (.+?)(?:\n|$)"),
    ]

    for pattern in error_patterns:
        match = pattern.search(section_text)
        if match:
            error_msg = match.group(0).strip()
            return normalize_failure_fingerprint(error_msg)

    # Fallback: use first non-empty line
    lines = [line.strip() for line in section_text.split('\n') if line.strip()]
    if lines:
        return normalize_failure_fingerprint(lines[0][:200])

    return None
