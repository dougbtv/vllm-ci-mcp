"""Configuration constants for vLLM CI monitoring."""

from pathlib import Path
from typing import Optional

# Default pipeline configuration
DEFAULT_PIPELINE = "vllm/ci"
DEFAULT_REPO = "vllm-project/vllm"
DEFAULT_BRANCH = "main"

# Buildkite API configuration
BUILDKITE_ORG = "vllm"  # Default org slug
BUILDKITE_API_BASE = "https://api.buildkite.com/v2"
BUILDKITE_ANALYTICS_BASE = "https://api.buildkite.com/v2/analytics"
BK_API_TIMEOUT = 30  # seconds
BK_LOG_API_TIMEOUT = 60  # seconds

# CLI timeouts (seconds) - for gh and git
GH_ISSUE_TIMEOUT = 30
GIT_BLAME_TIMEOUT = 10

# Legacy timeout constants (kept for backward compatibility)
BK_BUILD_TIMEOUT = BK_API_TIMEOUT
BK_JOB_TIMEOUT = BK_API_TIMEOUT
BK_LOG_TIMEOUT = BK_LOG_API_TIMEOUT

# Parsing limits
MAX_LOG_SNIPPET_LENGTH = 500
MAX_STACK_TRACE_LENGTH = 1000
MAX_ERROR_MESSAGE_LENGTH = 200

# Processing limits
MAX_FAILED_JOBS_TO_PROCESS = 10  # Limit to avoid timeouts

# GitHub issue matching
CI_FAILURE_LABEL = "ci-failure"  # Label used to track known CI failures
MIN_MATCH_CONFIDENCE = 0.6  # Minimum confidence to classify as KNOWN_TRACKED
EXACT_MATCH_CONFIDENCE = 0.9  # Confidence for exact title matches
FUZZY_MATCH_CONFIDENCE = 0.7  # Confidence for partial matches
WEAK_MATCH_CONFIDENCE = 0.5  # Confidence for keyword-only matches

# Optional repo path for ownership inference
# Can be set via VLLM_REPO_PATH environment variable
VLLM_REPO_PATH: Optional[Path] = None

# Test history budgets
MAX_BUILDS_FOR_TEST_HISTORY = 50  # Default lookback (commit-level tracking)
MAX_JOBS_PER_BUILD_FOR_TEST_HISTORY = 20  # Avoid scanning hundreds of jobs
MAX_LOG_BYTES_FOR_TEST_HISTORY = 200_000  # 200KB total across all logs
ESTIMATED_LOG_SIZE_PER_JOB = 10_000  # Conservative estimate for budget planning
