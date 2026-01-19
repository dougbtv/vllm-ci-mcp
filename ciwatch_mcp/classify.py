"""Classification heuristics for test failures."""

import re
from typing import Optional

from .cli import CLIError, search_github_issues
from .config import (
    CI_FAILURE_LABEL,
    EXACT_MATCH_CONFIDENCE,
    FUZZY_MATCH_CONFIDENCE,
    MIN_MATCH_CONFIDENCE,
    WEAK_MATCH_CONFIDENCE,
)
from .models import FailureClassification, TestFailure
from .normalize import generate_failure_key

# Infrastructure failure patterns
INFRA_PATTERNS = [
    (re.compile(r"timeout|timed out", re.I), "timeout detected"),
    (re.compile(r"connection refused|network error", re.I), "network issue"),
    (re.compile(r"no space left on device|disk full", re.I), "disk space"),
    (re.compile(r"out of memory|OOM|CUDA out of memory", re.I), "OOM"),
    (re.compile(r"killed by signal|SIGKILL", re.I), "process killed"),
    (re.compile(r"cannot allocate memory", re.I), "memory allocation"),
    (re.compile(r"failed to download|download error", re.I), "download failure"),
    (re.compile(r"agent lost|lost connection to agent", re.I), "agent connection lost"),
]

# Flaky test indicators
FLAKY_PATTERNS = [
    (re.compile(r"flaky", re.I), "test name contains 'flaky'"),
    (re.compile(r"intermittent", re.I), "intermittent failure"),
    (re.compile(r"passed on retry", re.I), "passed on retry"),
]


def validate_issue_match(issue: dict, failure: TestFailure) -> tuple[bool, float]:
    """Validate if an issue actually matches the failure.

    Args:
        issue: GitHub issue dict with title and labels
        failure: TestFailure to match against

    Returns:
        Tuple of (is_valid, confidence_score)
        - is_valid: Whether this is a legitimate match
        - confidence_score: 0.5-0.9 based on match quality
    """
    title = issue.get("title", "").lower()
    test_name = failure.test_name.lower()
    job_name = failure.job_name.lower()

    # Extract labels
    labels = [l["name"].lower() for l in issue.get("labels", [])]

    # Must have ci-failure label to be valid
    if CI_FAILURE_LABEL not in labels:
        return (False, 0.0)

    # Strong match: test name appears in title
    # Handle pytest format like "tests/test_foo.py::test_bar"
    test_parts = test_name.split("::")
    if test_name in title:
        return (True, EXACT_MATCH_CONFIDENCE)

    # Check if any part of the test name is in the title
    for part in test_parts:
        if part and len(part) > 3 and part in title:
            return (True, EXACT_MATCH_CONFIDENCE)

    # Medium match: job name in title
    if job_name in title:
        return (True, FUZZY_MATCH_CONFIDENCE)

    # Weak match: has ci-failure label but no strong title match
    # This catches cases where keywords overlap
    return (True, WEAK_MATCH_CONFIDENCE)


def find_best_issue_match(
    failure: TestFailure, repo: str
) -> Optional[tuple[str, float, str]]:
    """Find best matching GitHub issue for a failure.

    Strategy:
    1. Try exact phrase match in title with ci-failure label
    2. Fall back to fuzzy search with ci-failure label
    3. Validate and score all matches
    4. Return best match above confidence threshold

    Args:
        failure: TestFailure to find matches for
        repo: GitHub repository (format: "owner/repo")

    Returns:
        Tuple of (issue_url, confidence, reason) or None if no good match
    """
    # Try exact title match first
    exact_query = f'"{failure.test_name}" in:title label:{CI_FAILURE_LABEL} is:issue is:open'

    try:
        exact_issues = search_github_issues(repo, exact_query, limit=3)

        for issue in exact_issues:
            is_valid, confidence = validate_issue_match(issue, failure)
            if is_valid and confidence >= MIN_MATCH_CONFIDENCE:
                return (
                    issue["url"],
                    confidence,
                    f"Exact match in {CI_FAILURE_LABEL} issue: {issue['title']}",
                )
    except CLIError:
        # gh CLI not available or query failed
        pass

    # Fallback: broader search with label filter
    broad_query = f'{failure.test_name} label:{CI_FAILURE_LABEL} is:issue is:open'

    try:
        broad_issues = search_github_issues(repo, broad_query, limit=5)

        # Score and rank all matches
        matches = []
        for issue in broad_issues:
            is_valid, confidence = validate_issue_match(issue, failure)
            if is_valid and confidence >= MIN_MATCH_CONFIDENCE:
                matches.append((issue, confidence))

        if matches:
            # Take highest confidence match
            best_issue, best_conf = max(matches, key=lambda x: x[1])
            return (
                best_issue["url"],
                best_conf,
                f"Matched {CI_FAILURE_LABEL} issue: {best_issue['title']}",
            )
    except CLIError:
        pass

    return None  # No valid match found


def classify_failure(
    failure: TestFailure,
    repo: str = "vllm-project/vllm",
    search_github: bool = True,
) -> FailureClassification:
    """Apply classification heuristics to a test failure.

    Classification priority:
    1. KNOWN_TRACKED - GitHub issue exists
    2. INFRA_SUSPECTED - Infrastructure patterns matched
    3. FLAKY_SUSPECTED - Flaky indicators found
    4. NEW_REGRESSION - No known cause
    5. NEEDS_HUMAN_TRIAGE - Insufficient data

    Args:
        failure: TestFailure to classify
        repo: GitHub repository (format: "owner/repo")
        search_github: Whether to search GitHub for issues

    Returns:
        FailureClassification with category, confidence, and reason
    """
    failure_key = generate_failure_key(failure)

    # 1. Check GitHub for existing issues using improved matching
    if search_github:
        try:
            match_result = find_best_issue_match(failure, repo)
            if match_result:
                github_issue, github_confidence, github_reason = match_result
                return FailureClassification(
                    failure_key=failure_key,
                    test_failure=failure,
                    category="KNOWN_TRACKED",
                    github_issue=github_issue,
                    confidence=github_confidence,  # Variable confidence based on match quality
                    reason=github_reason,
                )
        except CLIError:
            # gh CLI not available, continue with other checks
            pass

    # 2. Check for infrastructure patterns
    combined_log = "\n".join(
        filter(
            None,
            [
                failure.error_message,
                failure.stack_trace,
                failure.log_snippet,
            ],
        )
    )

    for pattern, description in INFRA_PATTERNS:
        if pattern.search(combined_log):
            return FailureClassification(
                failure_key=failure_key,
                test_failure=failure,
                category="INFRA_SUSPECTED",
                confidence=0.7,
                reason=f"Infrastructure issue detected: {description}",
            )

    # 3. Check for flaky indicators
    for pattern, description in FLAKY_PATTERNS:
        if pattern.search(failure.test_name) or pattern.search(combined_log):
            return FailureClassification(
                failure_key=failure_key,
                test_failure=failure,
                category="FLAKY_SUSPECTED",
                confidence=0.6,
                reason=f"Flaky test indicator: {description}",
            )

    # 4. Default to NEW_REGRESSION if we have error details
    if failure.error_message:
        return FailureClassification(
            failure_key=failure_key,
            test_failure=failure,
            category="NEW_REGRESSION",
            confidence=0.5,
            reason="New failure with no known pattern",
        )

    # 5. Insufficient data
    return FailureClassification(
        failure_key=failure_key,
        test_failure=failure,
        category="NEEDS_HUMAN_TRIAGE",
        confidence=0.3,
        reason="Insufficient data for automatic classification",
    )


def deduplicate_failures(
    failures: list[FailureClassification],
) -> list[FailureClassification]:
    """Remove duplicate failures based on failure_key.

    If multiple failures have same key, keep first one.

    Args:
        failures: List of FailureClassification models

    Returns:
        Deduplicated list
    """
    seen_keys = set()
    unique_failures = []

    for failure in failures:
        if failure.failure_key not in seen_keys:
            seen_keys.add(failure.failure_key)
            unique_failures.append(failure)

    return unique_failures
