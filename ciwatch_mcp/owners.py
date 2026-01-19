"""Ownership inference using CODEOWNERS and git blame."""

from pathlib import Path
from typing import Optional

from .cli import git_blame_file


def parse_codeowners(repo_path: Path) -> dict[str, str]:
    """Parse CODEOWNERS file into pattern -> owner mapping.

    Args:
        repo_path: Path to git repository

    Returns:
        Dict of {file_pattern: owner_email}
    """
    codeowners_paths = [
        repo_path / "CODEOWNERS",
        repo_path / ".github" / "CODEOWNERS",
        repo_path / "docs" / "CODEOWNERS",
    ]

    pattern_map = {}
    for path in codeowners_paths:
        if not path.exists():
            continue

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    parts = line.split()
                    if len(parts) >= 2:
                        pattern = parts[0]
                        # Handle @username or email
                        owner = parts[1].lstrip("@")
                        pattern_map[pattern] = owner
        except Exception:
            # Gracefully handle file read errors
            continue

    return pattern_map


def infer_owner(
    test_file_path: str, repo_path: Optional[Path] = None
) -> tuple[Optional[str], float]:
    """Infer owner for a test file.

    Returns (owner_email, confidence)

    Strategy:
    1. Try CODEOWNERS match (confidence 0.9)
    2. Fall back to git blame (confidence 0.6)
    3. Return (None, 0.0) if repo_path not provided

    Args:
        test_file_path: Path to test file (from test name)
        repo_path: Optional path to git repository

    Returns:
        Tuple of (owner_email or None, confidence 0.0-1.0)
    """
    if not repo_path or not repo_path.exists():
        return (None, 0.0)

    # 1. Check CODEOWNERS
    codeowners = parse_codeowners(repo_path)
    for pattern, owner in codeowners.items():
        # Simple glob matching (could use fnmatch for better accuracy)
        # Check if pattern matches the test file path
        pattern_clean = pattern.lstrip("/")

        # Direct match or prefix match
        if pattern_clean in test_file_path or test_file_path.startswith(pattern_clean):
            return (owner, 0.9)

        # Wildcard matching for patterns like "tests/*"
        if "*" in pattern_clean:
            # Simple wildcard support
            pattern_prefix = pattern_clean.replace("*", "")
            if test_file_path.startswith(pattern_prefix):
                return (owner, 0.9)

    # 2. Fall back to git blame
    owner = git_blame_file(repo_path, test_file_path)
    if owner:
        return (owner, 0.6)

    return (None, 0.0)
