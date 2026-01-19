"""Markdown rendering for scan results."""

from .models import ScanResult


def render_daily_findings(result: ScanResult) -> str:
    """Render detailed Daily Findings report.

    Format:
    # Daily Findings - [Date]

    ## Summary
    - Build: [link]
    - Total Jobs: X, Failed: Y
    - Total Unique Failures: Z

    ## Failures by Category

    ### NEW_REGRESSION (N failures)
    - **test_name** in `job_name`
      - Error: error_message
      - Reason: classification reason
      - [Link to logs]

    ### KNOWN_TRACKED (N failures)
    ...

    Args:
        result: ScanResult model

    Returns:
        Markdown-formatted string
    """
    md = []
    md.append(f"# Daily Findings - {result.scan_timestamp.strftime('%Y-%m-%d')}\n")

    # Summary
    md.append("## Summary\n")
    md.append(
        f"- **Build**: [{result.build_info.build_number}]({result.build_info.build_url})"
    )
    md.append(f"- **Branch**: {result.build_info.branch}")
    md.append(f"- **Commit**: `{result.build_info.commit[:8]}`")
    md.append(
        f"- **Total Jobs**: {result.total_jobs}, **Failed**: {result.failed_jobs}"
    )
    md.append(f"- **Unique Failures**: {len(result.failures)}\n")

    # Group by category
    by_category: dict[str, list] = {}
    for failure in result.failures:
        category = failure.category
        if category not in by_category:
            by_category[category] = []
        by_category[category].append(failure)

    # Render each category in priority order
    md.append("## Failures by Category\n")

    category_order = [
        "NEW_REGRESSION",
        "FLAKY_SUSPECTED",
        "INFRA_SUSPECTED",
        "KNOWN_TRACKED",
        "NEEDS_HUMAN_TRIAGE",
    ]

    for category in category_order:
        if category not in by_category:
            continue

        failures = by_category[category]
        md.append(f"### {category} ({len(failures)} failures)\n")

        for f in failures:
            md.append(f"- **{f.test_failure.test_name}** in `{f.test_failure.job_name}`")

            if f.test_failure.error_message:
                # Truncate long error messages
                error_preview = f.test_failure.error_message[:100]
                if len(f.test_failure.error_message) > 100:
                    error_preview += "..."
                md.append(f"  - Error: `{error_preview}`")

            md.append(f"  - Reason: {f.reason}")
            md.append(f"  - Confidence: {f.confidence:.0%}")

            if f.github_issue:
                md.append(f"  - GitHub Issue: {f.github_issue}")

            if f.owner:
                confidence_str = (
                    f"{f.owner_confidence:.0%}" if f.owner_confidence else "unknown"
                )
                md.append(f"  - Owner: {f.owner} (confidence: {confidence_str})")

            md.append("")  # blank line between failures

    return "\n".join(md)


def render_standup_summary(result: ScanResult) -> str:
    """Render concise 1-3 line standup summary.

    Format:
    Nightly build [#123] failed with X failures: Y NEW_REGRESSION, Z INFRA_SUSPECTED.
    Key issues: [test1, test2]. [Link]

    Args:
        result: ScanResult model

    Returns:
        Concise summary string
    """
    # Count by category
    by_category: dict[str, int] = {}
    for failure in result.failures:
        category = failure.category
        by_category[category] = by_category.get(category, 0) + 1

    # Build summary line
    summary_parts = []
    for category in ["NEW_REGRESSION", "FLAKY_SUSPECTED", "INFRA_SUSPECTED"]:
        if category in by_category:
            summary_parts.append(f"{by_category[category]} {category}")

    # Key NEW_REGRESSION items (max 3)
    new_regressions = [
        f.test_failure.test_name.split("::")[-1]  # just test name
        for f in result.failures
        if f.category == "NEW_REGRESSION"
    ][:3]

    lines = []

    # Main status line
    state_str = (
        "PASSED" if result.build_info.state == "passed" else result.build_info.state.upper()
    )
    lines.append(
        f"Nightly build [{result.build_info.build_number}]({result.build_info.build_url}) "
        f"{state_str} with {len(result.failures)} unique failures"
    )

    # Add category breakdown if there are failures
    if summary_parts:
        lines[0] += f": {', '.join(summary_parts)}."

    # Add key NEW_REGRESSION tests
    if new_regressions:
        lines.append(f"Key NEW_REGRESSION tests: {', '.join(new_regressions)}")

    return " ".join(lines)
