"""Test history assessment and classification logic."""

from typing import Optional


def calculate_fail_rate(timeline: list[dict], start_idx: int = 0, end_idx: Optional[int] = None) -> float:
    """Calculate failure rate within a timeline window.

    Args:
        timeline: List of timeline entries (from oldest to newest)
        start_idx: Start index (inclusive)
        end_idx: End index (exclusive), None means end of list

    Returns:
        Failure rate as float 0.0-1.0
    """
    if end_idx is None:
        end_idx = len(timeline)

    window = timeline[start_idx:end_idx]
    found = [t for t in window if t["test_found"]]

    if not found:
        return 0.0

    failed = sum(1 for t in found if t["test_status"] == "fail")
    return failed / len(found)


def find_transition_point(timeline: list[dict]) -> Optional[int]:
    """Find pass→fail transition point in timeline.

    Returns index where test transitioned from mostly passing to mostly failing.

    Args:
        timeline: List of timeline entries (from oldest to newest, test_found=True)

    Returns:
        Index of transition point, or None if no clear transition
    """
    if len(timeline) < 3:
        return None

    # Look for a point where fail rate changes from <20% to >80%
    for i in range(1, len(timeline)):
        before_fail_rate = calculate_fail_rate(timeline, 0, i)
        after_fail_rate = calculate_fail_rate(timeline, i)

        if before_fail_rate < 0.2 and after_fail_rate > 0.8:
            return i

    return None


def consistent_fingerprint_after(timeline: list[dict], start_idx: int) -> bool:
    """Check if failures after start_idx have consistent fingerprint.

    Args:
        timeline: List of timeline entries (test_found=True)
        start_idx: Index to start checking from

    Returns:
        True if >80% of failures share same fingerprint
    """
    failures_after = []
    for t in timeline[start_idx:]:
        if t["test_status"] == "fail" and t.get("jobs"):
            for job in t["jobs"]:
                if job.get("fingerprint_normalized"):
                    failures_after.append(job["fingerprint_normalized"])

    if not failures_after:
        return False

    # Count most common fingerprint
    from collections import Counter
    fingerprint_counts = Counter(failures_after)
    most_common_count = fingerprint_counts.most_common(1)[0][1]

    return most_common_count / len(failures_after) > 0.8


def assess_test_history(timeline: list[dict]) -> dict:
    """Analyze timeline to classify test behavior.

    Args:
        timeline: List of timeline entries with keys:
            - test_found: bool
            - test_status: "pass" | "fail" | "unknown"
            - jobs: list of job outcomes with fingerprints

    Returns:
        Assessment dict with keys:
        - classification: str (REGRESSION | FLAKE_ONSET | PERSISTENT_FAIL | SPORADIC | INSUFFICIENT_DATA)
        - confidence: str (HIGH | MED | LOW)
        - notes: list[str] - explanatory notes
        - transition_build: Optional[int] - build number where regression occurred
    """
    # Filter to entries where test was found
    found = [t for t in timeline if t["test_found"]]

    if len(found) < 3:
        return {
            "classification": "INSUFFICIENT_DATA",
            "confidence": "LOW",
            "notes": [
                f"Test found in only {len(found)} builds",
                "Need at least 3 builds to detect patterns",
            ],
            "transition_build": None,
        }

    # Calculate overall fail rate
    fail_rate = calculate_fail_rate(found)

    # Check for regression (pass→fail transition)
    transition_idx = find_transition_point(found)
    if transition_idx is not None:
        transition_build = found[transition_idx]
        if consistent_fingerprint_after(found, transition_idx):
            return {
                "classification": "REGRESSION",
                "confidence": "HIGH",
                "notes": [
                    f"Clear transition at build {transition_build['build_number']} (commit {transition_build.get('commit_sha', 'unknown')[:7]})",
                    f"Consistent failure fingerprint across {len(found) - transition_idx} builds after transition",
                    f"Fail rate before: {calculate_fail_rate(found, 0, transition_idx):.1%}",
                    f"Fail rate after: {calculate_fail_rate(found, transition_idx):.1%}",
                ],
                "transition_build": transition_build["build_number"],
            }

    # Check for flake pattern (alternating outcomes)
    if 0.2 <= fail_rate <= 0.8:
        # Check if failures have varying fingerprints (indicates flake)
        all_fingerprints = []
        for t in found:
            if t["test_status"] == "fail" and t.get("jobs"):
                for job in t["jobs"]:
                    if job.get("fingerprint_normalized"):
                        all_fingerprints.append(job["fingerprint_normalized"])

        if all_fingerprints:
            from collections import Counter
            fingerprint_counts = Counter(all_fingerprints)
            unique_fingerprints = len(fingerprint_counts)

            if unique_fingerprints > 1:
                return {
                    "classification": "FLAKE_ONSET",
                    "confidence": "MED",
                    "notes": [
                        f"Intermittent failures: {fail_rate:.1%} fail rate",
                        f"{unique_fingerprints} different failure fingerprints detected",
                        "Test alternates between passing and failing",
                    ],
                    "transition_build": None,
                }

        return {
            "classification": "SPORADIC",
            "confidence": "MED",
            "notes": [
                f"Intermittent failures: {fail_rate:.1%} fail rate",
                "Occasional failures without clear pattern",
            ],
            "transition_build": None,
        }

    # Persistent failure
    if fail_rate > 0.8:
        # Check if all failures have same fingerprint
        all_fingerprints = []
        for t in found:
            if t["test_status"] == "fail" and t.get("jobs"):
                for job in t["jobs"]:
                    if job.get("fingerprint_normalized"):
                        all_fingerprints.append(job["fingerprint_normalized"])

        consistent = False
        if all_fingerprints:
            from collections import Counter
            fingerprint_counts = Counter(all_fingerprints)
            most_common_count = fingerprint_counts.most_common(1)[0][1]
            consistent = most_common_count / len(all_fingerprints) > 0.8

        return {
            "classification": "PERSISTENT_FAIL",
            "confidence": "HIGH",
            "notes": [
                f"Failing in {fail_rate:.1%} of recent builds",
                f"Consistent fingerprint: {consistent}",
                "Test has been broken for extended period",
            ],
            "transition_build": None,
        }

    # Mostly passing with rare failures
    return {
        "classification": "SPORADIC",
        "confidence": "HIGH",
        "notes": [
            f"Rare failures: {fail_rate:.1%} fail rate",
            "Test is mostly stable with occasional failures",
        ],
        "transition_build": None,
    }


def generate_summary(test_nodeid: str, timeline: list[dict], assessment: dict) -> str:
    """Generate human-readable summary for Slack/terminal.

    Args:
        test_nodeid: Full pytest nodeid
        timeline: Timeline data
        assessment: Assessment dict from assess_test_history

    Returns:
        Markdown-formatted summary string
    """
    classification = assessment["classification"]
    confidence = assessment["confidence"]

    # Build header
    lines = [
        f"## Test History: `{test_nodeid}`",
        "",
        f"**Classification:** {classification} (confidence: {confidence})",
        "",
    ]

    # Add notes
    if assessment.get("notes"):
        lines.append("**Analysis:**")
        for note in assessment["notes"]:
            lines.append(f"- {note}")
        lines.append("")

    # Add transition info if present
    if assessment.get("transition_build"):
        transition_build = assessment["transition_build"]
        # Find the transition entry in timeline
        for t in timeline:
            if t["build_number"] == transition_build:
                lines.append(f"**Regression introduced at:**")
                lines.append(f"- Build: [{transition_build}]({t['build_url']})")
                lines.append(f"- Commit: {t.get('commit_sha', 'unknown')[:7]}")
                if t.get("created_at"):
                    lines.append(f"- Time: {t['created_at']}")
                lines.append("")
                break

    # Add timeline summary
    found = [t for t in timeline if t["test_found"]]
    if found:
        failed_count = sum(1 for t in found if t["test_status"] == "fail")
        passed_count = sum(1 for t in found if t["test_status"] == "pass")
        lines.append(f"**Timeline summary:** {len(found)} builds scanned")
        lines.append(f"- Passed: {passed_count}")
        lines.append(f"- Failed: {failed_count}")
        lines.append("")

        # Show recent history (last 5 builds)
        lines.append("**Recent builds:**")
        recent = found[-5:] if len(found) > 5 else found
        for t in reversed(recent):  # Show newest first
            status_emoji = "✅" if t["test_status"] == "pass" else "❌"
            commit = t.get("commit_sha", "unknown")[:7]
            lines.append(f"- {status_emoji} Build [{t['build_number']}]({t['build_url']}) (commit {commit})")

    return "\n".join(lines)
