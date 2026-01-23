"""Subprocess wrappers for CLI tools (gh, git)."""

import json
import subprocess
from pathlib import Path
from typing import Optional

from .config import (
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
