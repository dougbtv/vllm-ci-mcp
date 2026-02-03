"""MCP server for vLLM CI monitoring."""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .buildkite_api import BuildkiteAPIError, BuildkiteClient
from .classify import classify_failure, deduplicate_failures
from .cli import CLIError
from .config import (
    DEFAULT_BRANCH,
    DEFAULT_MAIN_ANALYSIS_BUILDS,
    DEFAULT_MAIN_ANALYSIS_HOURS,
    DEFAULT_PERSISTENT_THRESHOLD,
    DEFAULT_PIPELINE,
    DEFAULT_REPO,
    MAX_BUILDS_FOR_TEST_HISTORY,
    MAX_FAILED_JOBS_TO_PROCESS,
    VLLM_REPO_PATH,
)
from .models import (
    FailureClassificationWithRecurrence,
    JobTestFailure,
    JobTestFailuresResult,
    MainBranchAnalysisResult,
    ScanResult,
    TestAnalyticsInfo,
    TestAnalyticsBulkResult,
)
from .normalize import (
    extract_test_failures_from_log,
    parse_build_json,
    parse_job_json,
    parse_test_nodeid,
)
from .owners import infer_owner
from .render import render_daily_findings, render_standup_summary
from .test_history import get_test_history

# Initialize FastMCP server
mcp = FastMCP("vLLM CI Watch")


def _apply_detail_level(failures: list, detail_level: str) -> list[dict]:
    """Apply detail level filtering to failure output.

    Reduces MCP response size by stripping verbose fields based on detail level.
    Expected token savings for typical nightly scan with 10 failures:
    - minimal: ~8k tokens (from ~10k to ~2k)
    - summary: ~5k tokens (from ~10k to ~5k)
    - full: 0 tokens (no reduction, includes daily_findings_text and standup_summary_text)

    Args:
        failures: List of FailureClassification objects
        detail_level: "minimal", "summary", or "full"

    Returns:
        List of dicts with appropriate fields based on detail level
    """
    result = []
    for failure in failures:
        failure_dict = failure.model_dump()

        if detail_level == "minimal":
            # Minimal: just test names and categories, no logs/errors
            failure_dict["test_failure"]["error_message"] = None
            failure_dict["test_failure"]["stack_trace"] = None
            failure_dict["test_failure"]["log_snippet"] = None
            failure_dict["github_issue"] = None
            failure_dict["reason"] = None
        elif detail_level == "summary":
            # Summary: keep error messages, truncate snippets, remove stack traces
            failure_dict["test_failure"]["stack_trace"] = None
            if failure_dict["test_failure"]["log_snippet"]:
                snippet = failure_dict["test_failure"]["log_snippet"]
                if len(snippet) > 200:
                    failure_dict["test_failure"]["log_snippet"] = snippet[:200] + "..."
        # else: detail_level == "full", keep everything

        result.append(failure_dict)

    return result


def _analyze_regression(runs: list[dict]) -> dict:
    """Analyze run history to find potential regression commit.

    Args:
        runs: List of run dicts (should be sorted chronologically)

    Returns:
        Dict with regression analysis
    """
    if not runs or len(runs) < 2:
        return {
            "regression_detected": False,
            "note": "Insufficient data for regression analysis",
        }

    # Find first failure after a series of passes
    # (Simple heuristic: 3+ passes, then a failure)
    passing_streak = 0
    regression_commit = None
    regression_timestamp = None

    for i, run in enumerate(runs):
        status = run.get("status", "").lower()

        if status in ["passed", "pass"]:
            passing_streak += 1
        elif status in ["failed", "fail"]:
            if passing_streak >= 3:
                # Likely regression
                regression_commit = run.get("commit_sha", "unknown")
                regression_timestamp = run.get("created_at")
                break
            passing_streak = 0

    if regression_commit:
        return {
            "regression_detected": True,
            "likely_commit": regression_commit[:8],
            "timestamp": regression_timestamp,
            "confidence": "medium",
            "note": f"Test failed after {passing_streak} consecutive passes",
        }

    # Check for flakiness (alternating pass/fail)
    if len(runs) >= 10:
        statuses = [r.get("status", "").lower() for r in runs[:10]]
        failures = sum(1 for s in statuses if s in ["failed", "fail"])

        if 2 <= failures <= 8:
            return {
                "regression_detected": False,
                "flaky_detected": True,
                "fail_rate": f"{failures}/10",
                "note": "Test shows intermittent failures (flaky behavior)",
            }

    return {
        "regression_detected": False,
        "note": "No clear regression pattern found",
    }


def _match_job_by_name(
    job_name_or_id: str,
    jobs: list[dict],
    strategy: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Match job by name or ID using specified strategy.

    Args:
        job_name_or_id: Job name pattern or UUID
        jobs: List of job dicts from build
        strategy: "exact", "fuzzy", or "id"

    Returns:
        (matched_job, error_message)
        If successful: (job_dict, None)
        If failed: (None, error_message)
    """
    if strategy == "id":
        # Direct ID lookup
        for job in jobs:
            if job.get("id") == job_name_or_id:
                return job, None
        return None, f"Job ID {job_name_or_id} not found"

    elif strategy == "exact":
        # Case-sensitive exact match
        matches = [j for j in jobs if j.get("name") == job_name_or_id]
        if len(matches) == 1:
            return matches[0], None
        elif len(matches) == 0:
            return None, f"Job '{job_name_or_id}' not found"
        else:
            return None, f"Multiple exact matches for '{job_name_or_id}'"

    elif strategy == "fuzzy":
        # Case-insensitive substring match
        pattern = job_name_or_id.lower()
        matches = [j for j in jobs if pattern in j.get("name", "").lower()]

        if len(matches) == 0:
            available = [j.get("name") for j in jobs]
            return None, f"No jobs match '{job_name_or_id}'. Available: {available}"
        elif len(matches) == 1:
            return matches[0], None
        else:
            # Multiple matches - check if one is an exact match (prefer it)
            exact_matches = [j for j in matches if j.get("name", "").lower() == pattern]
            if len(exact_matches) == 1:
                return exact_matches[0], None

            # No exact match or multiple exact matches - return candidates
            candidates = [{"id": j["id"], "name": j["name"]} for j in matches]
            return None, f"Multiple matches. Candidates: {candidates}"

    return None, f"Unknown match strategy: {strategy}"


@mcp.resource("prompt://ci-watch-daily")
def get_ci_watch_prompt() -> str:
    """CI Watch daily prompt for scanning nightly builds."""
    return """I'm on CI watch today, for vLLM! My role is to look at latest nightly build and assess if I need to take action.

Use ciwatch.scan_latest_nightly (pipeline vllm/ci, branch main, repo vllm-project/vllm, search_github=true).

Then give me:

- the Daily Findings output (copy/paste ready)
- the Standup summary output (copy/paste ready)

For soft failed tests, just briefly list. Focus on hard failures, those are the only ones where I am required to take action."""


@mcp.tool(name="ciwatch.scan_latest_nightly")
async def scan_latest_nightly(
    pipeline: str = DEFAULT_PIPELINE,
    branch: str = DEFAULT_BRANCH,
    repo: str = DEFAULT_REPO,
    search_github: bool = True,
    detail_level: str = "summary",
    max_failures: int = 50,
) -> dict:
    """Scan the latest nightly build for failures.

    Args:
        pipeline: Buildkite pipeline (default: vllm/ci)
        branch: Git branch to scan (default: main)
        repo: GitHub repo for issue search (default: vllm-project/vllm)
        search_github: Whether to search GitHub for matching issues
        detail_level: Output detail level - "minimal", "summary", or "full" (default: summary)
        max_failures: Maximum number of failures to return (default: 50)

    Returns:
        Dict with build_info, failures, daily_findings_text, standup_summary_text

        The "failures" list contains already-extracted, deduplicated test failures.
        Each failure has:
        - test_failure.test_name: The pytest nodeid (e.g., "tests/foo.py::test_bar")
        - category: Classification (NEW_REGRESSION, KNOWN_TRACKED, etc.)
        - test_failure.job_name: Which job it came from

    IMPORTANT - Next step to classify flaky vs real issues:

        # Check if failures have pytest nodeids (contain "::")
        pytest_failures = [f for f in result["failures"]
                          if "::" in f.get("test_failure", {}).get("test_name", "")]

        if pytest_failures:
            # Scan already has pytest test nodeids - use them directly!
            test_nodeids = [f["test_failure"]["test_name"] for f in pytest_failures]
            analytics = await get_test_analytics_bulk(test_nodeids)
            # Report flaky vs new regressions
        else:
            # Scan only has job-level failures - these are typically infrastructure failures
            # (docker builds, environment setup, etc.) that don't need analytics checking
            # Report them as infrastructure issues, not test regressions
    """
    try:
        # Initialize Buildkite client
        client = BuildkiteClient()

        # Get repo path from env if set
        repo_path = None
        repo_path_str = os.environ.get("VLLM_REPO_PATH")
        if repo_path_str:
            repo_path = Path(repo_path_str)

        # 1. Get latest nightly build
        # Nightly builds are identified by source="schedule" (more reliable than message text)
        # Use created_from to narrow search window (nightlies run daily, so check last 2 days)
        from datetime import datetime, timedelta, timezone
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

        builds_data = client.list_builds(
            pipeline=pipeline,
            branch=branch,
            limit=100,  # Generous limit, created_from keeps actual results ~70-80 builds
            created_from=two_days_ago,
        )

        if not builds_data:
            return {"error": "No builds found"}

        # Filter for scheduled builds (nightly/daily runs) that are analyzable
        # Accept: passed, failed, failing, canceled (exclude: scheduled, running, canceling)
        # "failing" = build in progress but has failures (good enough for CI watch)
        analyzable_states = ["passed", "failed", "failing", "canceled"]
        nightly_builds = [
            b for b in builds_data
            if b.get("source") == "schedule"
            and b.get("state") in analyzable_states
        ]

        if not nightly_builds:
            # Fallback: try scheduled builds without state filter
            nightly_builds = [b for b in builds_data if b.get("source") == "schedule"]

        if not nightly_builds:
            # Final fallback: latest build in analyzable state
            analyzable_builds = [b for b in builds_data if b.get("state") in analyzable_states]
            nightly_builds = analyzable_builds[:1] if analyzable_builds else builds_data[:1]

        build_info = parse_build_json(nightly_builds[0])

        # 2. Get all jobs for this build
        build_data = client.get_build(pipeline=pipeline, build_number=build_info.build_number)
        jobs_data = build_data.get("jobs", [])

        jobs = [parse_job_json(j, build_info.build_number) for j in jobs_data]
        failed_jobs = [j for j in jobs if not j.passed]

        # 3. Extract failures from failed jobs (limit to avoid timeouts)
        all_failures = []
        for job in failed_jobs[:MAX_FAILED_JOBS_TO_PROCESS]:
            try:
                log_text = client.get_job_log(
                    pipeline=pipeline,
                    build_number=build_info.build_number,
                    job_id=job.job_id,
                )

                test_failures = extract_test_failures_from_log(log_text, job.job_name)

                # Classify each failure
                for test_failure in test_failures:
                    classified = classify_failure(
                        test_failure, repo=repo, search_github=search_github
                    )

                    # Optional: infer owner
                    if repo_path:
                        # Extract test file path from test name (e.g., "tests/foo.py::test_bar" -> "tests/foo.py")
                        test_file = test_failure.test_name.split("::")[0]
                        owner, confidence = infer_owner(test_file, repo_path)
                        classified.owner = owner
                        classified.owner_confidence = confidence

                    all_failures.append(classified)

            except BuildkiteAPIError as e:
                # Log fetch failed, skip this job but continue
                continue

        # 4. Deduplicate
        unique_failures = deduplicate_failures(all_failures)

        # 5. Build result
        result = ScanResult(
            build_info=build_info,
            total_jobs=len(jobs),
            failed_jobs=len(failed_jobs),
            failures=unique_failures,
            scan_timestamp=datetime.now(),
        )

        # 6. Apply detail level filtering
        failures_output = _apply_detail_level(unique_failures[:max_failures], detail_level)

        # 7. Build base response
        response = {
            "build_info": result.build_info.model_dump(),
            "total_jobs": result.total_jobs,
            "failed_jobs": result.failed_jobs,
            "failures": failures_output,
            "scan_timestamp": result.scan_timestamp.isoformat(),
        }

        # 8. Add rendered text only in full mode
        if detail_level == "full":
            daily_findings = render_daily_findings(result, jobs=jobs)
            standup_summary = render_standup_summary(result, jobs=jobs)
            response["daily_findings_text"] = daily_findings
            response["standup_summary_text"] = standup_summary

        return response

    except BuildkiteAPIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@mcp.tool(name="ciwatch.analyze_main_branch")
async def analyze_main_branch(
    pipeline: str = DEFAULT_PIPELINE,
    branch: str = DEFAULT_BRANCH,
    repo: str = DEFAULT_REPO,
    search_github: bool = True,
    detail_level: str = "summary",
    max_builds: int = DEFAULT_MAIN_ANALYSIS_BUILDS,
    max_failures: int = 50,
    hours_lookback: int = DEFAULT_MAIN_ANALYSIS_HOURS,
    exclude_scheduled: bool = True,
) -> dict:
    """Analyze recent failures on main branch from commit-triggered builds.

    Unlike scan_latest_nightly which analyzes a single scheduled build, this function
    scans multiple recent commit-triggered builds to identify what tests are currently
    failing on main and how frequently they fail.

    Args:
        pipeline: Buildkite pipeline (default: vllm/ci)
        branch: Branch to analyze (default: main)
        repo: GitHub repo for issue search (default: vllm-project/vllm)
        search_github: Whether to search GitHub for known issues (default: True)
        detail_level: Output detail level - "minimal", "summary", or "full" (default: summary)
        max_builds: Maximum number of builds to analyze (default: 5)
        max_failures: Maximum number of unique failures to return (default: 50)
        hours_lookback: Time window in hours to search for builds (default: 24)
        exclude_scheduled: Exclude scheduled/nightly builds (default: True)

    Returns:
        Dict with analysis_window, summary, failures (with recurrence data), scan_timestamp

        Example workflow:
        1. Call analyze_main_branch(hours_lookback=24, max_builds=5)
        2. Extract pytest nodeids from failures
        3. Call get_test_analytics_bulk(nodeids) to check flakiness
        4. Classify: flaky (ignore), new regressions (investigate), persistent (critical)
    """
    try:
        client = BuildkiteClient()
        repo_path = Path(os.getenv("VLLM_REPO_PATH", "")) if os.getenv("VLLM_REPO_PATH") else VLLM_REPO_PATH

        # Calculate time window
        from datetime import timedelta, timezone

        now = datetime.now(timezone.utc)
        start_time = now - timedelta(hours=hours_lookback)

        # 1. Fetch builds in the time window (over-fetch to account for filtering)
        builds_data = client.list_builds(
            pipeline=pipeline,
            branch=branch,
            created_from=start_time.isoformat(),
            limit=100,  # Over-fetch to ensure we get enough after filtering
        )

        if not builds_data:
            return {
                "error": f"No builds found in the last {hours_lookback} hours",
                "analysis_window": {
                    "start_time": start_time.isoformat(),
                    "end_time": now.isoformat(),
                    "hours_lookback": hours_lookback,
                },
            }

        # 2. Filter builds
        analyzable_states = ["passed", "failed", "failing", "canceled"]
        filtered_builds = []

        for build in builds_data:
            # Skip if not in analyzable state
            if build.get("state") not in analyzable_states:
                continue

            # Skip scheduled builds if requested
            if exclude_scheduled and build.get("source") == "schedule":
                continue

            # Skip passed builds (optimization - only process failed/failing)
            if build.get("state") == "passed":
                continue

            filtered_builds.append(build)

            # Stop once we have enough
            if len(filtered_builds) >= max_builds:
                break

        if not filtered_builds:
            return {
                "error": f"No failed builds found in the last {hours_lookback} hours (after filtering)",
                "analysis_window": {
                    "start_time": start_time.isoformat(),
                    "end_time": now.isoformat(),
                    "hours_lookback": hours_lookback,
                    "total_builds_in_window": len(builds_data),
                },
            }

        # 3. Process each build and collect failures
        all_failures = []
        build_summaries = []
        builds_scanned = 0

        for build_data in filtered_builds:
            build_number = str(build_data.get("number", ""))
            commit = build_data.get("commit", "")[:8]
            state = build_data.get("state", "")
            created_at = build_data.get("created_at", "")

            build_summaries.append({
                "build_number": build_number,
                "commit": commit,
                "state": state,
                "created_at": created_at,
            })

            try:
                # Get full build data with jobs
                full_build = client.get_build(pipeline=pipeline, build_number=build_number)
                jobs_data = full_build.get("jobs", [])

                # Parse and filter to failed jobs
                jobs = [parse_job_json(j, build_number) for j in jobs_data]
                failed_jobs = [j for j in jobs if not j.passed]

                # Extract failures from failed jobs (limit to avoid timeouts)
                for job in failed_jobs[:MAX_FAILED_JOBS_TO_PROCESS]:
                    try:
                        log_text = client.get_job_log(
                            pipeline=pipeline,
                            build_number=build_number,
                            job_id=job.job_id,
                        )

                        test_failures = extract_test_failures_from_log(log_text, job.job_name)

                        # Classify each failure
                        for test_failure in test_failures:
                            classified = classify_failure(
                                test_failure, repo=repo, search_github=search_github
                            )

                            # Optional: infer owner
                            if repo_path:
                                test_file = test_failure.test_name.split("::")[0]
                                owner, confidence = infer_owner(test_file, repo_path)
                                classified.owner = owner
                                classified.owner_confidence = confidence

                            # Track build context
                            classified_dict = classified.model_dump()
                            classified_dict["_build_number"] = build_number
                            classified_dict["_commit"] = commit
                            all_failures.append(classified_dict)

                    except BuildkiteAPIError:
                        # Log fetch failed, skip this job but continue
                        continue

                builds_scanned += 1

            except BuildkiteAPIError:
                # Build fetch failed, skip this build but continue
                continue

        if not all_failures:
            return {
                "analysis_window": {
                    "start_time": start_time.isoformat(),
                    "end_time": now.isoformat(),
                    "hours_lookback": hours_lookback,
                    "builds_scanned": builds_scanned,
                    "builds_analyzed": build_summaries,
                },
                "summary": {
                    "total_builds_scanned": builds_scanned,
                    "builds_with_failures": 0,
                    "total_unique_failures": 0,
                    "persistent_failures": 0,
                    "intermittent_failures": 0,
                },
                "failures": [],
                "scan_timestamp": now.isoformat(),
            }

        # 4. Deduplicate and aggregate
        # Group by failure_key to count occurrences
        from collections import defaultdict

        failure_groups = defaultdict(list)
        for failure_dict in all_failures:
            key = failure_dict["failure_key"]
            failure_groups[key].append(failure_dict)

        # Build aggregated failures with recurrence data
        aggregated_failures = []
        for key, failures_list in failure_groups.items():
            # Use first failure as base
            base_failure = failures_list[0]

            # Extract build context
            seen_builds = list(set(f["_build_number"] for f in failures_list))
            seen_commits = list(set(f["_commit"] for f in failures_list))

            # Create extended classification
            occurrence_count = len(seen_builds)
            recurrence_rate = occurrence_count / builds_scanned if builds_scanned > 0 else 0.0

            # Remove temporary build tracking fields
            base_failure.pop("_build_number", None)
            base_failure.pop("_commit", None)

            # Add recurrence fields
            base_failure["occurrence_count"] = occurrence_count
            base_failure["seen_in_builds"] = seen_builds
            base_failure["seen_in_commits"] = seen_commits
            base_failure["recurrence_rate"] = round(recurrence_rate, 2)

            aggregated_failures.append(base_failure)

        # Sort by recurrence_rate descending, then by occurrence_count
        aggregated_failures.sort(
            key=lambda f: (f["recurrence_rate"], f["occurrence_count"]),
            reverse=True,
        )

        # Apply max_failures limit
        aggregated_failures = aggregated_failures[:max_failures]

        # 5. Build summary stats
        builds_with_failures = len([b for b in build_summaries if any(
            f["_build_number"] == b["build_number"] for f in all_failures
        )])

        persistent_count = sum(1 for f in aggregated_failures if f["recurrence_rate"] >= DEFAULT_PERSISTENT_THRESHOLD)
        intermittent_count = len(aggregated_failures) - persistent_count

        summary = {
            "total_builds_scanned": builds_scanned,
            "builds_with_failures": builds_with_failures,
            "total_unique_failures": len(aggregated_failures),
            "persistent_failures": persistent_count,
            "intermittent_failures": intermittent_count,
        }

        # 6. Apply detail level filtering
        # Convert dict format to model format for _apply_detail_level
        from .models import FailureClassification

        failure_models = []
        for f_dict in aggregated_failures:
            # Create base FailureClassification (without recurrence fields)
            base_dict = {k: v for k, v in f_dict.items() if k not in [
                "occurrence_count", "seen_in_builds", "seen_in_commits", "recurrence_rate"
            ]}
            base_model = FailureClassification(**base_dict)
            failure_models.append(base_model)

        failures_output = _apply_detail_level(failure_models, detail_level)

        # Re-add recurrence fields after detail filtering
        for i, f_dict in enumerate(aggregated_failures):
            failures_output[i]["occurrence_count"] = f_dict["occurrence_count"]
            failures_output[i]["seen_in_builds"] = f_dict["seen_in_builds"]
            failures_output[i]["seen_in_commits"] = f_dict["seen_in_commits"]
            failures_output[i]["recurrence_rate"] = f_dict["recurrence_rate"]

        # 7. Build response
        response = {
            "analysis_window": {
                "start_time": start_time.isoformat(),
                "end_time": now.isoformat(),
                "hours_lookback": hours_lookback,
                "builds_scanned": builds_scanned,
                "builds_analyzed": build_summaries,
            },
            "summary": summary,
            "failures": failures_output,
            "scan_timestamp": now.isoformat(),
        }

        # 8. Add rendered text only in full mode
        if detail_level == "full":
            # Build a simple text summary
            text_lines = [
                f"# Main Branch Analysis ({hours_lookback}h lookback)",
                f"",
                f"**Builds scanned:** {builds_scanned}",
                f"**Unique failures:** {len(aggregated_failures)}",
                f"**Persistent failures (≥50% recurrence):** {persistent_count}",
                f"**Intermittent failures:** {intermittent_count}",
                f"",
                f"## Top Failures by Recurrence:",
            ]

            for i, failure in enumerate(aggregated_failures[:10], 1):
                test_name = failure["test_failure"]["test_name"]
                recurrence = failure["recurrence_rate"]
                count = failure["occurrence_count"]
                category = failure["category"]
                text_lines.append(
                    f"{i}. `{test_name}` - {recurrence*100:.0f}% ({count}/{builds_scanned}) [{category}]"
                )

            response["analysis_text"] = "\n".join(text_lines)

        return response

    except BuildkiteAPIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@mcp.tool(name="ciwatch.scan_build")
async def scan_build(
    build_id_or_url: str,
    pipeline: str = DEFAULT_PIPELINE,
    repo: str = DEFAULT_REPO,
    search_github: bool = True,
    detail_level: str = "summary",
    max_failures: int = 50,
) -> dict:
    """Scan a specific build by number or URL.

    Args:
        build_id_or_url: Build number (e.g., "12345") or URL
        pipeline: Buildkite pipeline
        repo: GitHub repo for issue search
        search_github: Whether to search GitHub
        detail_level: Output detail level - "minimal", "summary", or "full" (default: summary)
        max_failures: Maximum number of failures to return (default: 50)

    Returns:
        Dict with build_info, failures, daily_findings_text, standup_summary_text

        The "failures" list contains already-extracted, deduplicated test failures.
        Each failure has:
        - test_failure.test_name: The pytest nodeid (e.g., "tests/foo.py::test_bar")
        - category: Classification (NEW_REGRESSION, KNOWN_TRACKED, etc.)
        - test_failure.job_name: Which job it came from

    IMPORTANT - Next step to classify flaky vs real issues:

        # Check if failures have pytest nodeids (contain "::")
        pytest_failures = [f for f in result["failures"]
                          if "::" in f.get("test_failure", {}).get("test_name", "")]

        if pytest_failures:
            # Scan already has pytest test nodeids - use them directly!
            test_nodeids = [f["test_failure"]["test_name"] for f in pytest_failures]
            analytics = await get_test_analytics_bulk(test_nodeids)
            # Report flaky vs new regressions
        else:
            # Scan only has job-level failures - these are typically infrastructure failures
            # (docker builds, environment setup, etc.) that don't need analytics checking
            # Report them as infrastructure issues, not test regressions
    """
    try:
        # Initialize Buildkite client
        client = BuildkiteClient()

        # Get repo path from env if set
        repo_path = None
        repo_path_str = os.environ.get("VLLM_REPO_PATH")
        if repo_path_str:
            repo_path = Path(repo_path_str)

        # If build_id_or_url is URL, extract number
        build_number = build_id_or_url
        if build_id_or_url.startswith("http"):
            # Parse URL to extract build number
            match = re.search(r"/builds/(\d+)", build_id_or_url)
            if match:
                build_number = match.group(1)
            else:
                return {"error": "Could not parse build number from URL"}

        # Get build data directly from API
        build_data = client.get_build(pipeline=pipeline, build_number=build_number)
        jobs_data = build_data.get("jobs", [])

        if not jobs_data:
            return {"error": f"No jobs found for build {build_number}"}

        # Parse jobs
        jobs = [parse_job_json(j, build_number) for j in jobs_data]
        failed_jobs = [j for j in jobs if not j.passed]

        # Build a basic BuildInfo from what we can infer
        # (We don't have full build metadata without fetching it separately)
        build_info_dict = {
            "build_number": build_number,
            "build_url": f"https://buildkite.com/{pipeline}/builds/{build_number}",
            "pipeline": pipeline,
            "branch": "unknown",
            "commit": "unknown",
            "state": "unknown",
            "created_at": datetime.now().isoformat(),
            "finished_at": None,
        }
        build_info = parse_build_json(build_info_dict)

        # Extract failures from failed jobs (limit to avoid timeouts)
        all_failures = []
        for job in failed_jobs[:MAX_FAILED_JOBS_TO_PROCESS]:
            try:
                log_text = client.get_job_log(
                    pipeline=pipeline, build_number=build_number, job_id=job.job_id
                )

                test_failures = extract_test_failures_from_log(log_text, job.job_name)

                # Classify each failure
                for test_failure in test_failures:
                    classified = classify_failure(
                        test_failure, repo=repo, search_github=search_github
                    )

                    # Optional: infer owner
                    if repo_path:
                        test_file = test_failure.test_name.split("::")[0]
                        owner, confidence = infer_owner(test_file, repo_path)
                        classified.owner = owner
                        classified.owner_confidence = confidence

                    all_failures.append(classified)

            except BuildkiteAPIError:
                # Log fetch failed, skip this job
                continue

        # Deduplicate
        unique_failures = deduplicate_failures(all_failures)

        # Build result
        result = ScanResult(
            build_info=build_info,
            total_jobs=len(jobs),
            failed_jobs=len(failed_jobs),
            failures=unique_failures,
            scan_timestamp=datetime.now(),
        )

        # Apply detail level filtering
        failures_output = _apply_detail_level(unique_failures[:max_failures], detail_level)

        # Build base response
        response = {
            "build_info": result.build_info.model_dump(),
            "total_jobs": result.total_jobs,
            "failed_jobs": result.failed_jobs,
            "failures": failures_output,
            "scan_timestamp": result.scan_timestamp.isoformat(),
        }

        # Add rendered text only in full mode
        if detail_level == "full":
            daily_findings = render_daily_findings(result, jobs=jobs)
            standup_summary = render_standup_summary(result, jobs=jobs)
            response["daily_findings_text"] = daily_findings
            response["standup_summary_text"] = standup_summary

        return response

    except BuildkiteAPIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@mcp.tool(name="ciwatch.render")
async def render(scan_result: dict, format: str = "daily_findings") -> str:
    """Render scan results as formatted text.

    Args:
        scan_result: ScanResult dict from scan_latest_nightly or scan_build
        format: Output format - "daily_findings" or "standup"

    Returns:
        Formatted text output
    """
    try:
        result = ScanResult(**scan_result)

        if format == "daily_findings":
            return render_daily_findings(result)
        elif format == "standup":
            return render_standup_summary(result)
        else:
            return f"Unknown format: {format}. Use 'daily_findings' or 'standup'."

    except Exception as e:
        return f"Error rendering: {str(e)}"


@mcp.tool(name="ciwatch.test_history")
async def test_history(
    test_nodeid: str,
    branch: str = DEFAULT_BRANCH,
    pipeline: str = DEFAULT_PIPELINE,
    build_query: Optional[str] = None,
    lookback_builds: int = MAX_BUILDS_FOR_TEST_HISTORY,
    job_filter: Optional[str] = None,
    include_logs: bool = True,
) -> dict:
    """Track test outcome history across recent builds (log-based).

    NOTE: For faster results with built-in flaky detection, use ciwatch.test_history_analytics.
    This tool parses logs and is resource-intensive.

    Args:
        test_nodeid: Full pytest nodeid (e.g., "tests/test_foo.py::test_bar")
        branch: Git branch (default: main)
        pipeline: Buildkite pipeline (default: vllm/ci)
        build_query: Optional message filter (e.g., "nightly"). DEPRECATED - may cause timeouts
        lookback_builds: Number of recent builds to scan (default: 50)
        job_filter: Optional job name filter (e.g., "Distributed Tests")
        include_logs: Include log excerpts in output (default: True)

    Returns:
        Dict with timeline (commit-level granularity), assessment, and summary
    """
    try:
        return await get_test_history(
            test_nodeid=test_nodeid,
            branch=branch,
            pipeline=pipeline,
            build_query=build_query,
            lookback_builds=lookback_builds,
            job_filter=job_filter,
            include_logs=include_logs,
        )
    except CLIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@mcp.tool(name="ciwatch.test_history_analytics")
async def test_history_analytics(
    test_name_or_nodeid: str,
    suite_slug: str = "ci-1",
) -> dict:
    """Check if a test is flagged as flaky in Buildkite Test Analytics.

    Much faster than log parsing - uses pre-computed analytics data.
    Note: The Buildkite Analytics REST API currently has limited endpoints.
    Full run history is not available via REST API.

    Args:
        test_name_or_nodeid: Test name (e.g., "test_foo") OR
                             full nodeid (e.g., "tests/foo.py::test_bar[param]")
        suite_slug: Test suite slug (default: ci-1)

    Returns:
        Dict with test info and flaky status
    """
    try:
        # Initialize Buildkite client
        client = BuildkiteClient()

        # Parse if nodeid (contains "::")
        scope = None
        test_name = test_name_or_nodeid
        if "::" in test_name_or_nodeid:
            scope, test_name = parse_test_nodeid(test_name_or_nodeid)

        # Search for test in all tests
        all_tests = client.list_analytics_tests(suite_slug=suite_slug, limit=100)

        # Match test by name (with optional scope filter)
        matching_tests = []
        for t in all_tests:
            # Check if name matches
            if test_name not in t.get("name", ""):
                continue
            # If we have a scope, filter by it
            if scope and t.get("scope") != scope:
                continue
            matching_tests.append(t)

        if not matching_tests:
            # Try checking flaky tests specifically
            flaky_tests = client.list_analytics_tests(suite_slug=suite_slug, state="flaky", limit=100)
            flaky_matches = []
            for t in flaky_tests:
                if test_name not in t.get("name", ""):
                    continue
                if scope and t.get("scope") != scope:
                    continue
                flaky_matches.append(t)

            if flaky_matches:
                matching_tests = flaky_matches
            else:
                search_term = f"'{test_name_or_nodeid}'"
                return {
                    "error": f"Test {search_term} not found in Test Analytics",
                    "suggestion": "Try partial name or check if test exists in suite",
                }

        # If multiple matches, return list for user to choose
        if len(matching_tests) > 1:
            return {
                "error": "Multiple tests match",
                "matches": [
                    {
                        "id": t["id"],
                        "name": t["name"],
                        "scope": t.get("scope", ""),
                        "location": t.get("location", ""),
                    }
                    for t in matching_tests
                ],
            }

        test = matching_tests[0]

        # Check if test appears in flaky list
        flaky_tests = client.list_analytics_tests(suite_slug=suite_slug, state="flaky", limit=100)
        is_flaky = any(t["id"] == test["id"] for t in flaky_tests)

        # Check if test appears in recently failed list
        failed_tests = client.list_analytics_tests(suite_slug=suite_slug, order="recently_failed", limit=100)
        recently_failed = any(t["id"] == test["id"] for t in failed_tests[:20])

        return {
            "test_name": test["name"],
            "test_location": test.get("location", ""),
            "test_id": test["id"],
            "web_url": test.get("web_url", ""),
            "is_flaky": is_flaky,
            "recently_failed": recently_failed,
            "note": "Full run history and reliability metrics are not available via Buildkite Analytics REST API",
            "suggestion": f"View detailed analytics in web UI: {test.get('web_url', 'N/A')}",
        }

    except BuildkiteAPIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@mcp.tool(name="ciwatch.get_job_test_failures")
async def get_job_test_failures(
    build_id_or_url: str,
    job_name_or_id: str,
    pipeline: str = DEFAULT_PIPELINE,
    match_strategy: str = "fuzzy",
) -> dict:
    """Extract pytest test failures from a specific job's logs.

    When to use this:
        - When scan_latest_nightly/scan_build doesn't provide enough detail about a job
        - When you want to see ALL tests from a specific job (not just deduplicated failures)
        - When investigating a particular job mentioned in scan results

    Note: Only works for pytest-running jobs. Infrastructure jobs (docker build, etc.)
    won't have test output. Look for jobs with "Test" in the name.

    Args:
        build_id_or_url: Build number (e.g., "48161") or full Buildkite URL
        job_name_or_id: Job name (e.g., "Entrypoints Test") or job UUID
        pipeline: Buildkite pipeline slug (default: vllm/ci)
        match_strategy:
            - "exact": Case-sensitive exact match on job name
            - "fuzzy": Case-insensitive substring match (default)
            - "id": Treat job_name_or_id as job UUID directly

    Returns:
        Dict with job_info, test_failures, and total_failures

    Workflow tip:
        After extracting test failures, check if they're flaky:

        test_nodeids = [f["test_nodeid"] for f in result["test_failures"]]
        analytics = await get_test_analytics_bulk(test_nodeids)
    """
    try:
        # Initialize Buildkite client
        client = BuildkiteClient()

        # Parse build number from URL if needed
        build_number = build_id_or_url
        if build_id_or_url.startswith("http"):
            match = re.search(r"/builds/(\d+)", build_id_or_url)
            if match:
                build_number = match.group(1)
            else:
                return {"error": "Could not parse build number from URL"}

        # Get build data to find the job
        build_data = client.get_build(pipeline=pipeline, build_number=build_number)
        jobs_data = build_data.get("jobs", [])

        if not jobs_data:
            return {"error": f"No jobs found for build {build_number}"}

        # Match job using strategy
        matched_job, error_msg = _match_job_by_name(job_name_or_id, jobs_data, match_strategy)
        if not matched_job:
            return {"error": error_msg}

        # Extract job info
        job_info = {
            "job_id": matched_job["id"],
            "job_name": matched_job["name"],
            "job_url": matched_job.get("web_url", ""),
            "state": matched_job.get("state", "unknown"),
            "exit_status": matched_job.get("exit_status"),
        }

        # Fetch job log
        try:
            log_text = client.get_job_log(
                pipeline=pipeline,
                build_number=build_number,
                job_id=matched_job["id"],
            )
        except BuildkiteAPIError as e:
            return {"error": f"Could not fetch job log: {str(e)}"}

        # Extract test failures from log
        test_failures = extract_test_failures_from_log(log_text, matched_job["name"])

        # Convert to JobTestFailure models with parsed nodeid
        job_test_failures = []
        for tf in test_failures:
            scope, test_name = parse_test_nodeid(tf.test_name)
            job_test_failures.append(
                JobTestFailure(
                    test_nodeid=tf.test_name,
                    scope=scope,
                    test_name=test_name,
                    error_message=tf.error_message,
                    stack_trace=tf.stack_trace,
                    log_snippet=tf.log_snippet,
                )
            )

        # Build result
        result = JobTestFailuresResult(
            job_info=job_info,
            test_failures=job_test_failures,
            total_failures=len(job_test_failures),
        )

        return result.model_dump()

    except BuildkiteAPIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@mcp.tool(name="ciwatch.get_test_analytics_bulk")
async def get_test_analytics_bulk(
    test_nodeids: list[str],
    suite_slug: str = "ci-1",
) -> dict:
    """Get Buildkite Analytics data for multiple tests in batch.

    Use this to classify test failures as flaky vs new regressions.
    Fast batch operation - only 2-3 API calls regardless of test count.

    Args:
        test_nodeids: List of full pytest nodeids
                      (e.g., ["tests/foo.py::test_bar", "tests/baz.py::test_qux[param]"])
        suite_slug: Test suite slug (default: ci-1)

    Returns:
        Dict with results, not_found, multiple_matches, total_checked, and warnings

        - results: Tests found in analytics with is_flaky flag
        - not_found: Tests not in analytics (likely new tests or regressions)
        - multiple_matches: Tests with ambiguous matches (rare)

    Interpretation:
        - is_flaky=true: Known flaky test, can likely ignore
        - not_found: New test or new regression, needs investigation
        - recently_failed=true: Has failed recently (last 20 runs)
    """
    try:
        # Initialize Buildkite client
        client = BuildkiteClient()

        # Warn if too many tests
        warnings = []
        if len(test_nodeids) > 50:
            warnings.append(f"Large batch ({len(test_nodeids)} tests) may be slow. Consider splitting.")

        # Parse all nodeids into (scope, name) tuples
        parsed_tests = []
        for nodeid in test_nodeids:
            scope, name = parse_test_nodeid(nodeid)
            parsed_tests.append((nodeid, scope, name))

        # Batch fetch all tests from Analytics
        all_tests = client.list_analytics_tests(suite_slug=suite_slug, limit=100)

        # Batch fetch flaky tests
        flaky_tests = client.list_analytics_tests(suite_slug=suite_slug, state="flaky", limit=100)
        flaky_ids = {t["id"] for t in flaky_tests}

        # Batch fetch recently failed tests
        failed_tests = client.list_analytics_tests(
            suite_slug=suite_slug, order="recently_failed", limit=100
        )
        recently_failed_ids = {t["id"] for t in failed_tests[:20]}

        # Match each input nodeid
        results = []
        not_found = []
        multiple_matches = {}

        for nodeid, scope, test_name in parsed_tests:
            # Find matching tests in analytics
            # Strategy: exact scope match, fuzzy name match for parametrized tests
            matches = []
            for test in all_tests:
                test_scope = test.get("scope", "")
                test_location = test.get("location", "")
                analytics_name = test.get("name", "")

                # Scope must match exactly (if we have a scope)
                if scope and test_scope and scope != test_scope:
                    continue

                # For name matching:
                # - Exact match: test_name == analytics_name
                # - Parametrized match: base name matches (e.g., "test_bar" in "test_bar[param1]")
                # - Base match: analytics_name matches if test_name is base (no brackets)

                # Extract base name (without parameters)
                base_name = test_name.split("[")[0] if "[" in test_name else test_name
                analytics_base = analytics_name.split("[")[0] if "[" in analytics_name else analytics_name

                if test_name == analytics_name or base_name == analytics_base:
                    matches.append(test)

            if len(matches) == 0:
                not_found.append(nodeid)
            elif len(matches) == 1:
                test = matches[0]
                results.append(
                    TestAnalyticsInfo(
                        test_nodeid=nodeid,
                        test_id=test["id"],
                        test_name=test["name"],
                        scope=test.get("scope", scope),
                        location=test.get("location"),
                        web_url=test.get("web_url"),
                        is_flaky=test["id"] in flaky_ids,
                        recently_failed=test["id"] in recently_failed_ids,
                        note=None,
                    )
                )
            else:
                # Multiple matches
                multiple_matches[nodeid] = [
                    {
                        "id": t["id"],
                        "name": t["name"],
                        "scope": t.get("scope", ""),
                        "location": t.get("location", ""),
                    }
                    for t in matches
                ]

        # Build result
        result = TestAnalyticsBulkResult(
            results=results,
            not_found=not_found,
            multiple_matches=multiple_matches,
            total_checked=len(test_nodeids),
            warnings=warnings,
        )

        return result.model_dump()

    except BuildkiteAPIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


def main():
    """CLI entry point for running MCP server."""
    # Allow setting VLLM_REPO_PATH via env
    repo_path_str = os.environ.get("VLLM_REPO_PATH")
    if repo_path_str:
        print(f"Using VLLM repo path: {repo_path_str}")

    mcp.run()


if __name__ == "__main__":
    main()
