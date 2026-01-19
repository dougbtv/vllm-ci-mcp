"""Classification heuristics for test failures."""

import re
from typing import Optional

from .cli import CLIError, search_github_issues
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

    # 1. Check GitHub for existing issues
    github_issue = None
    if search_github:
        try:
            # Search by test name
            issues = search_github_issues(
                repo=repo,
                query=f"{failure.test_name} is:issue is:open",
                limit=5,
            )
            if issues:
                # Use first matching issue
                github_issue = issues[0]["url"]
                return FailureClassification(
                    failure_key=failure_key,
                    test_failure=failure,
                    category="KNOWN_TRACKED",
                    github_issue=github_issue,
                    confidence=0.8,
                    reason=f"Existing GitHub issue: {issues[0]['title']}",
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
