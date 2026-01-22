"""Subprocess wrappers for CLI tools (bk, gh, git)."""

import json
import subprocess
from pathlib import Path
from typing import Optional

from .config import (
    BK_BUILD_TIMEOUT,
    BK_JOB_TIMEOUT,
    BK_LOG_TIMEOUT,
    GH_ISSUE_TIMEOUT,
    GIT_BLAME_TIMEOUT,
)


class CLIError(Exception):
    """Raised when CLI tool is missing or fails."""

    pass


def check_cli_available(tool: str) -> bool:
    """Check if CLI tool exists in PATH."""
    result = subprocess.run(["which", tool], capture_output=True)
    return result.returncode == 0


def run_bk_build_list(
    pipeline: str,
    branch: str = "main",
    limit: int = 1,
    state: Optional[str] = None,
    message_filter: Optional[str] = None,
) -> list[dict]:
    """Get build list as JSON from Buildkite CLI.

    Args:
        pipeline: Pipeline slug (e.g., "vllm/ci")
        branch: Git branch to filter by
        limit: Number of builds to return
        state: Optional state filter (e.g., "failed", "passed")
        message_filter: Optional message content filter (e.g., "nightly")

    Returns:
        List of build dicts from bk CLI

    Raises:
        CLIError: If bk not available or command fails
    """
    if not check_cli_available("bk"):
        raise CLIError(
            "bk CLI not found. Install with: brew install buildkite/buildkite/bk"
        )

    cmd = [
        "bk",
        "build",
        "list",
        "--pipeline",
        pipeline,
        "--branch",
        branch,
        "--limit",
        str(limit),
        "--output",
        "json",
    ]

    if state:
        cmd.extend(["--state", state])

    if message_filter:
        cmd.extend(["--message", message_filter])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=BK_BUILD_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise CLIError(f"bk build list timed out after {BK_BUILD_TIMEOUT}s")

    if result.returncode != 0:
        raise CLIError(f"bk build list failed: {result.stderr}")

    # Parse JSON output
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CLIError(f"Failed to parse bk build list JSON: {e}")


def run_bk_job_list(
    pipeline: str, build_number: str, state: Optional[str] = None
) -> list[dict]:
    """Get job list for a specific build.

    Args:
        pipeline: Pipeline slug
        build_number: Build number
        state: Optional state filter

    Returns:
        List of job dicts from bk CLI

    Raises:
        CLIError: If bk not available or command fails
    """
    if not check_cli_available("bk"):
        raise CLIError("bk CLI not found")

    cmd = [
        "bk",
        "build",
        "view",
        build_number,
        "--pipeline",
        pipeline,
        "--output",
        "json",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=BK_JOB_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise CLIError(f"bk build view timed out after {BK_JOB_TIMEOUT}s")

    if result.returncode != 0:
        raise CLIError(f"bk build view failed: {result.stderr}")

    try:
        build_data = json.loads(result.stdout)
        jobs = build_data.get("jobs", [])

        # Filter by state if requested
        if state:
            jobs = [j for j in jobs if j.get("state") == state]

        return jobs
    except json.JSONDecodeError as e:
        raise CLIError(f"Failed to parse bk build view JSON: {e}")


def run_bk_job_log(pipeline: str, build_number: str, job_id: str) -> str:
    """Fetch raw log text for a job.

    Args:
        pipeline: Pipeline slug
        build_number: Build number
        job_id: Job ID

    Returns:
        Raw log text (string)

    Raises:
        CLIError: If bk not available or command fails
    """
    if not check_cli_available("bk"):
        raise CLIError("bk CLI not found")

    cmd = [
        "bk",
        "job",
        "log",
        job_id,
        "--pipeline",
        pipeline,
        "--build-number",
        build_number,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=BK_LOG_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise CLIError(f"bk job log timed out after {BK_LOG_TIMEOUT}s")

    if result.returncode != 0:
        raise CLIError(f"bk job log failed: {result.stderr}")

    return result.stdout


def run_bk_analytics_tests(
    suite_slug: str = "ci-1",
    order: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Fetch tests from Test Analytics API.

    Args:
        suite_slug: Test suite slug (default: ci-1)
        order: Sort order (recently_failed, slowest)
        state: Filter by state (flaky, failed)
        limit: Max results

    Returns:
        List of test dicts from Test Analytics API

    Raises:
        CLIError: If bk not available or command fails
    """
    if not check_cli_available("bk"):
        raise CLIError("bk CLI not found")

    cmd = ["bk", "api", "--analytics", f"/suites/{suite_slug}/tests"]

    params = []
    if order:
        params.append(f"order={order}")
    if state:
        params.append(f"state={state}")
    if limit:
        params.append(f"limit={limit}")

    if params:
        cmd[-1] = cmd[-1] + "?" + "&".join(params)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise CLIError("bk analytics API timed out after 30s")

    if result.returncode != 0:
        raise CLIError(f"bk analytics API failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CLIError(f"Failed to parse bk analytics JSON: {e}")


def run_bk_analytics_test_detail(
    suite_slug: str,
    test_id: str,
) -> dict:
    """Get detailed info for a specific test.

    Args:
        suite_slug: Test suite slug
        test_id: Test ID

    Returns:
        Test detail dict

    Raises:
        CLIError: If bk not available or command fails
    """
    if not check_cli_available("bk"):
        raise CLIError("bk CLI not found")

    cmd = ["bk", "api", "--analytics", f"/suites/{suite_slug}/tests/{test_id}"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise CLIError("bk analytics API timed out after 30s")

    if result.returncode != 0:
        raise CLIError(f"bk analytics API failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CLIError(f"Failed to parse bk analytics JSON: {e}")


def run_bk_analytics_test_runs(
    suite_slug: str,
    test_id: str,
    limit: int = 100,
) -> list[dict]:
    """Get run history for a specific test.

    Args:
        suite_slug: Test suite slug
        test_id: Test ID
        limit: Max results

    Returns:
        List of run dicts with commit_sha, created_at, status, etc.

    Raises:
        CLIError: If bk not available or command fails
    """
    if not check_cli_available("bk"):
        raise CLIError("bk CLI not found")

    cmd = [
        "bk",
        "api",
        "--analytics",
        f"/suites/{suite_slug}/tests/{test_id}/runs?limit={limit}",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise CLIError("bk analytics API timed out after 30s")

    if result.returncode != 0:
        raise CLIError(f"bk analytics API failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CLIError(f"Failed to parse bk analytics JSON: {e}")


def search_github_issues(
    repo: str, query: str, limit: int = 10
) -> list[dict]:
    """Search GitHub issues using gh CLI.

    Args:
        repo: Repository in format "owner/repo" (e.g., "vllm-project/vllm")
        query: Search query
        limit: Max number of results

    Returns:
        List of issue dicts with number, title, url, state, labels

    Raises:
        CLIError: If gh not available or command fails
    """
    if not check_cli_available("gh"):
        raise CLIError("gh CLI not found. Install with: brew install gh")

    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        query,
        "--limit",
        str(limit),
        "--json",
        "number,title,url,state,labels",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GH_ISSUE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise CLIError(f"gh issue list timed out after {GH_ISSUE_TIMEOUT}s")

    if result.returncode != 0:
        raise CLIError(f"gh issue list failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise CLIError(f"Failed to parse gh issue list JSON: {e}")


def git_blame_file(
    repo_path: Path, file_path: str, line_number: Optional[int] = None
) -> Optional[str]:
    """Get git blame for a file to infer ownership.

    Args:
        repo_path: Path to git repository
        file_path: Relative path to file within repo
        line_number: Optional specific line number

    Returns:
        Email of most recent committer, or None if git fails

    Raises:
        Does not raise - returns None on any failure for graceful degradation
    """
    if not repo_path.exists():
        return None

    cmd = ["git", "-C", str(repo_path), "blame", "--porcelain", file_path]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GIT_BLAME_TIMEOUT
        )

        if result.returncode != 0:
            return None

        # Parse porcelain format for author-mail
        for line in result.stdout.splitlines():
            if line.startswith("author-mail"):
                # Extract email from "author-mail <email@example.com>"
                email = line.split("<")[1].split(">")[0]
                return email

        return None
    except (subprocess.TimeoutExpired, Exception):
        # Graceful degradation on any error
        return None
