"""MCP server for vLLM CI monitoring."""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .buildkite_api import BuildkiteAPIError, BuildkiteClient
from .classify import classify_failure, deduplicate_failures
from .config import (
    DEFAULT_BRANCH,
    DEFAULT_PIPELINE,
    DEFAULT_REPO,
    MAX_BUILDS_FOR_TEST_HISTORY,
    MAX_FAILED_JOBS_TO_PROCESS,
    VLLM_REPO_PATH,
)
from .models import ScanResult
from .normalize import extract_test_failures_from_log, parse_build_json, parse_job_json
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
        # Note: message_filter not supported by API, so we get latest builds and filter client-side
        builds_data = client.list_builds(
            pipeline=pipeline,
            branch=branch,
            limit=20,  # Get more builds to ensure we find a completed nightly
        )

        if not builds_data:
            return {"error": "No builds found"}

        # Filter for nightly builds that are not in initial states
        # Accept: passed, failed, failing, canceled (exclude: scheduled, running, canceling)
        # "failing" = build in progress but has failures (good enough for CI watch)
        analyzable_states = ["passed", "failed", "failing", "canceled"]
        nightly_builds = [
            b for b in builds_data
            if "nightly" in b.get("message", "").lower()
            and b.get("state") in analyzable_states
        ]

        if not nightly_builds:
            # Fallback: try just "nightly" in message without state filter
            nightly_builds = [b for b in builds_data if "nightly" in b.get("message", "").lower()]

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
    test_name: str,
    suite_slug: str = "ci-1",
) -> dict:
    """Check if a test is flagged as flaky in Buildkite Test Analytics.

    Much faster than log parsing - uses pre-computed analytics data.
    Note: The Buildkite Analytics REST API currently has limited endpoints.
    Full run history is not available via REST API.

    Args:
        test_name: Test name to search for (e.g., "test_async_tp_pass_replace")
        suite_slug: Test suite slug (default: ci-1)

    Returns:
        Dict with test info and flaky status
    """
    try:
        # Initialize Buildkite client
        client = BuildkiteClient()

        # Search for test in all tests
        all_tests = client.list_analytics_tests(suite_slug=suite_slug, limit=100)

        # Match test by name
        matching_tests = [t for t in all_tests if test_name in t.get("name", "")]

        if not matching_tests:
            # Try checking flaky tests specifically
            flaky_tests = client.list_analytics_tests(suite_slug=suite_slug, state="flaky", limit=100)
            flaky_matches = [t for t in flaky_tests if test_name in t.get("name", "")]

            if flaky_matches:
                matching_tests = flaky_matches
            else:
                return {
                    "error": f"Test '{test_name}' not found in Test Analytics",
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


def main():
    """CLI entry point for running MCP server."""
    # Allow setting VLLM_REPO_PATH via env
    repo_path_str = os.environ.get("VLLM_REPO_PATH")
    if repo_path_str:
        print(f"Using VLLM repo path: {repo_path_str}")

    mcp.run()


if __name__ == "__main__":
    main()
