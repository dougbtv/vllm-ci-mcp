"""Configuration constants for vLLM CI monitoring."""

from pathlib import Path
from typing import Optional

# Default pipeline configuration
DEFAULT_PIPELINE = "vllm/ci"
DEFAULT_REPO = "vllm-project/vllm"
DEFAULT_BRANCH = "main"

# CLI timeouts (seconds)
BK_BUILD_TIMEOUT = 30
BK_JOB_TIMEOUT = 30
BK_LOG_TIMEOUT = 60
GH_ISSUE_TIMEOUT = 30
GIT_BLAME_TIMEOUT = 10

# Parsing limits
MAX_LOG_SNIPPET_LENGTH = 500
MAX_STACK_TRACE_LENGTH = 1000
MAX_ERROR_MESSAGE_LENGTH = 200

# Processing limits
MAX_FAILED_JOBS_TO_PROCESS = 10  # Limit to avoid timeouts

# Optional repo path for ownership inference
# Can be set via VLLM_REPO_PATH environment variable
VLLM_REPO_PATH: Optional[Path] = None
