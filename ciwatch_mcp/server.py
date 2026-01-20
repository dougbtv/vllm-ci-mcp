"""MCP server for vLLM CI monitoring."""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .classify import classify_failure, deduplicate_failures
from .cli import CLIError, run_bk_build_list, run_bk_job_list, run_bk_job_log
from .config import (
    DEFAULT_BRANCH,
    DEFAULT_PIPELINE,
    DEFAULT_REPO,
    MAX_FAILED_JOBS_TO_PROCESS,
    VLLM_REPO_PATH,
)
from .models import ScanResult
from .normalize import extract_test_failures_from_log, parse_build_json, parse_job_json
from .owners import infer_owner
from .render import render_daily_findings, render_standup_summary

# Initialize FastMCP server
mcp = FastMCP("vLLM CI Watch")


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
) -> dict:
    """Scan the latest nightly build for failures.

    Args:
        pipeline: Buildkite pipeline (default: vllm/ci)
        branch: Git branch to scan (default: main)
        repo: GitHub repo for issue search (default: vllm-project/vllm)
        search_github: Whether to search GitHub for matching issues

    Returns:
        Dict with build_info, failures, daily_findings_text, standup_summary_text
    """
    try:
        # Get repo path from env if set
        repo_path = None
        repo_path_str = os.environ.get("VLLM_REPO_PATH")
        if repo_path_str:
            repo_path = Path(repo_path_str)

        # 1. Get latest nightly build
        builds_data = run_bk_build_list(
            pipeline=pipeline,
            branch=branch,
            limit=1,
            message_filter="nightly"
        )

        if not builds_data:
            return {"error": "No builds found"}

        build_info = parse_build_json(builds_data[0])

        # 2. Get all jobs for this build
        jobs_data = run_bk_job_list(pipeline=pipeline, build_number=build_info.build_number)

        jobs = [parse_job_json(j, build_info.build_number) for j in jobs_data]
        failed_jobs = [j for j in jobs if not j.passed]

        # 3. Extract failures from failed jobs (limit to avoid timeouts)
        all_failures = []
        for job in failed_jobs[:MAX_FAILED_JOBS_TO_PROCESS]:
            try:
                log_text = run_bk_job_log(
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

            except CLIError as e:
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

        # 6. Render outputs
        daily_findings = render_daily_findings(result, jobs=jobs)
        standup_summary = render_standup_summary(result, jobs=jobs)

        # Return as dict with both structured data and rendered text
        return {
            "build_info": result.build_info.model_dump(),
            "total_jobs": result.total_jobs,
            "failed_jobs": result.failed_jobs,
            "failures": [f.model_dump() for f in result.failures],
            "scan_timestamp": result.scan_timestamp.isoformat(),
            "daily_findings_text": daily_findings,
            "standup_summary_text": standup_summary,
        }

    except CLIError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Unexpected error: {str(e)}"}


@mcp.tool(name="ciwatch.scan_build")
async def scan_build(
    build_id_or_url: str,
    pipeline: str = DEFAULT_PIPELINE,
    repo: str = DEFAULT_REPO,
    search_github: bool = True,
) -> dict:
    """Scan a specific build by number or URL.

    Args:
        build_id_or_url: Build number (e.g., "12345") or URL
        pipeline: Buildkite pipeline
        repo: GitHub repo for issue search
        search_github: Whether to search GitHub

    Returns:
        Dict with build_info, failures, daily_findings_text, standup_summary_text
    """
    try:
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

        # Get build data by fetching job list (which includes build info)
        jobs_data = run_bk_job_list(pipeline=pipeline, build_number=build_number)

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
                log_text = run_bk_job_log(
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

            except CLIError:
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

        # Render outputs
        daily_findings = render_daily_findings(result, jobs=jobs)
        standup_summary = render_standup_summary(result, jobs=jobs)

        return {
            "build_info": result.build_info.model_dump(),
            "total_jobs": result.total_jobs,
            "failed_jobs": result.failed_jobs,
            "failures": [f.model_dump() for f in result.failures],
            "scan_timestamp": result.scan_timestamp.isoformat(),
            "daily_findings_text": daily_findings,
            "standup_summary_text": standup_summary,
        }

    except CLIError as e:
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


def main():
    """CLI entry point for running MCP server."""
    # Allow setting VLLM_REPO_PATH via env
    repo_path_str = os.environ.get("VLLM_REPO_PATH")
    if repo_path_str:
        print(f"Using VLLM repo path: {repo_path_str}")

    mcp.run()


if __name__ == "__main__":
    main()
