"""Markdown rendering for scan results."""

from .models import FailureClassification, JobInfo, ScanResult


def is_soft_failure(failure: FailureClassification, jobs: list[JobInfo]) -> bool:
    """Check if a failure came from a soft_failed job.

    Args:
        failure: FailureClassification to check
        jobs: List of all JobInfo objects from the build

    Returns:
        True if the failure's job was soft_failed, False otherwise
    """
    job = next((j for j in jobs if j.job_name == failure.test_failure.job_name), None)
    return job.soft_failed if job else False


def render_daily_findings(result: ScanResult, jobs: list[JobInfo] | None = None) -> str:
    """Render detailed Daily Findings report.

    Format:
    # Daily Findings - [Date]

    ## Summary
    - Build: [link]
    - Total Jobs: X, Failed: Y (X hard / Y soft)
    - Total Unique Failures: Z

    ## Hard Failures (blocking builds)
    ...

    ## Soft Failures (optional tests, allowed to fail)
    ...

    Args:
        result: ScanResult model
        jobs: Optional list of JobInfo objects to determine soft vs hard failures

    Returns:
        Markdown-formatted string
    """
    md = []
    md.append(f"# Daily Findings - {result.scan_timestamp.strftime('%Y-%m-%d')}\n")

    # Separate hard and soft failures
    hard_failures = []
    soft_failures = []
    if jobs:
        for failure in result.failures:
            if is_soft_failure(failure, jobs):
                soft_failures.append(failure)
            else:
                hard_failures.append(failure)
    else:
        # Fallback if jobs not provided
        hard_failures = result.failures

    # Count hard vs soft failed jobs
    hard_failed_jobs = 0
    soft_failed_jobs = 0
    if jobs:
        for job in jobs:
            if not job.passed:
                if job.soft_failed:
                    soft_failed_jobs += 1
                else:
                    hard_failed_jobs += 1
    else:
        hard_failed_jobs = result.failed_jobs

    # Summary
    md.append("## Summary\n")
    md.append(
        f"- **Build**: [{result.build_info.build_number}]({result.build_info.build_url})"
    )
    md.append(f"- **Branch**: {result.build_info.branch}")
    md.append(f"- **Commit**: `{result.build_info.commit[:8]}`")
    md.append(
        f"- **Total Jobs**: {result.total_jobs}, **Failed**: {result.failed_jobs} "
        f"({hard_failed_jobs} hard / {soft_failed_jobs} soft)"
    )
    md.append(
        f"- **Unique Failures**: {len(result.failures)} "
        f"({len(hard_failures)} hard / {len(soft_failures)} soft)"
    )

    # Add build status context
    if result.build_info.state == "passed" and soft_failures and not hard_failures:
        md.append("- **Build Status**: PASSED (all failures are optional)")

    md.append("")

    # Helper function to render failures by category
    def render_failures_section(failures_list: list, section_title: str, compact: bool = False) -> None:
        """Render a section of failures grouped by category.

        Args:
            failures_list: List of failures to render
            section_title: Section heading
            compact: If True, use compact format (job name + issue link only)
        """
        if not failures_list:
            md.append(f"## {section_title} (0)\n")
            md.append("(none)\n")
            return

        md.append(f"## {section_title} ({len(failures_list)})\n")

        # Group by category
        by_category: dict[str, list] = {}
        for failure in failures_list:
            category = failure.category
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(failure)

        # Render each category in priority order
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
                # Compact format: just job name + issue link
                if compact:
                    issue_str = ""
                    if f.github_issue:
                        issue_str = f" - [{f.github_issue}]({f.github_issue})"
                    md.append(f"- **{f.test_failure.job_name}**{issue_str}")
                else:
                    # Detailed format: all info
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

    # Render hard failures first (detailed), then soft failures (compact)
    render_failures_section(hard_failures, "Hard Failures (blocking builds)", compact=False)
    render_failures_section(soft_failures, "Soft Failures (optional tests, allowed to fail)", compact=True)

    return "\n".join(md)


def render_standup_summary(result: ScanResult, jobs: list[JobInfo] | None = None) -> str:
    """Render concise 1-3 line standup summary.

    Format:
    Nightly build [#123] PASSED with X soft-failed (optional) tests: Y NEW_REGRESSION.
    Or: Nightly build [#123] FAILED with X failures (Y hard / Z soft): ...

    Args:
        result: ScanResult model
        jobs: Optional list of JobInfo objects to determine soft vs hard failures

    Returns:
        Concise summary string
    """
    # Separate hard and soft failures
    hard_failures = []
    soft_failures = []
    if jobs:
        for failure in result.failures:
            if is_soft_failure(failure, jobs):
                soft_failures.append(failure)
            else:
                hard_failures.append(failure)
    else:
        # Fallback if jobs not provided
        hard_failures = result.failures

    # Count by category for hard and soft
    hard_by_category: dict[str, int] = {}
    soft_by_category: dict[str, int] = {}
    for failure in hard_failures:
        category = failure.category
        hard_by_category[category] = hard_by_category.get(category, 0) + 1
    for failure in soft_failures:
        category = failure.category
        soft_by_category[category] = soft_by_category.get(category, 0) + 1

    lines = []

    # Main status line
    state_str = (
        "PASSED" if result.build_info.state == "passed" else result.build_info.state.upper()
    )

    # Special case: build passed but has soft failures
    if result.build_info.state == "passed" and soft_failures and not hard_failures:
        # Build summary parts from soft failures
        summary_parts = []
        for category in ["NEW_REGRESSION", "FLAKY_SUSPECTED", "INFRA_SUSPECTED"]:
            if category in soft_by_category:
                summary_parts.append(f"{soft_by_category[category]} {category}")

        lines.append(
            f"Nightly build [{result.build_info.build_number}]({result.build_info.build_url}) "
            f"{state_str} with {len(soft_failures)} soft-failed (optional) tests"
        )

        if summary_parts:
            lines[0] += f": {', '.join(summary_parts)}"
    else:
        # Build failed or has hard failures
        failure_context = ""
        if jobs and (hard_failures or soft_failures):
            failure_context = f" ({len(hard_failures)} hard / {len(soft_failures)} soft)"

        lines.append(
            f"Nightly build [{result.build_info.build_number}]({result.build_info.build_url}) "
            f"{state_str} with {len(result.failures)} unique failures{failure_context}"
        )

        # Build summary from hard failures primarily
        summary_parts = []
        for category in ["NEW_REGRESSION", "FLAKY_SUSPECTED", "INFRA_SUSPECTED"]:
            if category in hard_by_category:
                summary_parts.append(f"{hard_by_category[category]} {category}")

        if summary_parts:
            lines[0] += f": {', '.join(summary_parts)}."

    # Key NEW_REGRESSION items from hard failures (max 3)
    new_regressions = [
        f.test_failure.test_name.split("::")[-1]  # just test name
        for f in hard_failures
        if f.category == "NEW_REGRESSION"
    ][:3]

    if new_regressions:
        lines.append(f"Key NEW_REGRESSION tests: {', '.join(new_regressions)}")

    return " ".join(lines)
