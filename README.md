# vllm-ci-mcp

MCP Server for vLLM CI monitoring. Scans Buildkite builds, extracts failures, classifies them, and generates copy/paste friendly reports.

## Features

- Scan latest nightly builds or specific builds by ID/URL
- Extract test-level failures from pytest output
- Classify failures as:
  - `KNOWN_TRACKED` - Existing GitHub issue found
  - `INFRA_SUSPECTED` - Infrastructure patterns (timeout, OOM, network)
  - `FLAKY_SUSPECTED` - Flaky test indicators
  - `NEW_REGRESSION` - New failures requiring attention
  - `NEEDS_HUMAN_TRIAGE` - Insufficient data
- Infer test owners from CODEOWNERS and git blame
- Generate markdown reports:
  - Daily Findings: detailed breakdown
  - Standup Summary: concise 1-3 line summary
- Graceful degradation when tools are missing

## Prerequisites

- **Python 3.11+**
- **Buildkite CLI**: `brew install buildkite/buildkite/bk`
- **GitHub CLI** (optional): `brew install gh`
- **Git** (optional, for owner inference): usually pre-installed

## Installation

```bash
cd /home/doug/codebase/vllm-ci-mcp
pip install -e .

# Install dev dependencies
pip install -e ".[dev]"
```

## Authentication

### Buildkite

Set your Buildkite API token:

```bash
export BUILDKITE_TOKEN="your-buildkite-token"
```

Get a token from: https://buildkite.com/user/api-access-tokens

### GitHub (Optional)

Authenticate GitHub CLI:

```bash
gh auth login
```

### Repo Path (Optional)

For owner inference, set the path to your local vLLM checkout:

```bash
export VLLM_REPO_PATH=/path/to/vllm
```

## Running the MCP Server

### Local Development

```bash
# Run directly
python -m ciwatch_mcp.server

# Or use the installed script
ciwatch-mcp
```

### In Claude Code (CLI)

**Recommended: Use the `claude mcp add` command:**

```bash
# 1. Install the MCP server in development mode
cd /path/to/vllm-ci-mcp
pip install -e .

# 2. Navigate to this project directory and add the MCP server
cd /path/to/vllm-ci-mcp
claude mcp add --transport stdio vllm-ci-watch -- python -m ciwatch_mcp.server

# 3. Add environment variables to ~/.claude.json
# Find the vllm-ci-mcp project section and add env vars to the mcpServers entry:
# Edit manually or use:
python3 << 'EOF'
import json
config_path = "/home/doug/.claude.json"
with open(config_path) as f:
    config = json.load(f)

# Update the project-specific MCP server config
project_path = "/home/hdds/480ssd/codebase/vllm-ci-mcp"  # Adjust to your path
if project_path in config.get("projects", {}):
    if "vllm-ci-watch" in config["projects"][project_path].get("mcpServers", {}):
        config["projects"][project_path]["mcpServers"]["vllm-ci-watch"]["env"] = {
            "BUILDKITE_TOKEN": "your-buildkite-token-here",
            "VLLM_REPO_PATH": "/path/to/your/vllm/repo"
        }

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
print("Updated env vars for vllm-ci-watch")
EOF

# 4. Restart Claude Code
# Run: /mcp
# You should see "vllm-ci-watch" listed
```

**Alternative: Manual configuration in `~/.claude.json`:**

The `claude mcp add` command creates a project-specific config in `~/.claude.json`. You can also manually add it to the project's `mcpServers` section:

```json
{
  "projects": {
    "/path/to/vllm-ci-mcp": {
      "mcpServers": {
        "vllm-ci-watch": {
          "type": "stdio",
          "command": "python",
          "args": ["-m", "ciwatch_mcp.server"],
          "env": {
            "BUILDKITE_TOKEN": "your-token-here",
            "VLLM_REPO_PATH": "/path/to/vllm"
          }
        }
      }
    }
  }
}
```

**Testing the connection:**

Once Claude Code is restarted, you can test by asking:
- "Scan the latest vLLM nightly build"
- "Check build 47580 for failures"
- "What CI failures do we have?"

### In Claude Desktop

Add to your Claude Desktop MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "vllm-ci-watch": {
      "command": "python",
      "args": ["-m", "ciwatch_mcp.server"],
      "env": {
        "BUILDKITE_TOKEN": "your-token-here",
        "VLLM_REPO_PATH": "/path/to/vllm"
      }
    }
  }
}
```

## Usage

### MCP Tools

The server provides three MCP tools:

#### 1. `ciwatch.scan_latest_nightly`

Scan the latest nightly build.

**Parameters:**
- `pipeline` (str, default: `"vllm/ci"`): Buildkite pipeline slug
- `branch` (str, default: `"main"`): Git branch
- `repo` (str, default: `"vllm-project/vllm"`): GitHub repo for issue search
- `search_github` (bool, default: `true`): Whether to search GitHub for issues

**Returns:**
```json
{
  "build_info": {...},
  "total_jobs": 50,
  "failed_jobs": 5,
  "failures": [...],
  "daily_findings_text": "# Daily Findings...",
  "standup_summary_text": "Nightly build [#123]..."
}
```

**Example:**
```python
# In Claude Code or MCP client
result = await ciwatch.scan_latest_nightly()
print(result["daily_findings_text"])
```

#### 2. `ciwatch.scan_build`

Scan a specific build by number or URL.

**Parameters:**
- `build_id_or_url` (str, required): Build number (e.g., `"12345"`) or Buildkite URL
- `pipeline` (str, default: `"vllm/ci"`)
- `repo` (str, default: `"vllm-project/vllm"`)
- `search_github` (bool, default: `true`)

**Example:**
```python
result = await ciwatch.scan_build(
    build_id_or_url="https://buildkite.com/vllm/ci/builds/47580"
)
```

#### 3. `ciwatch.render`

Re-render a scan result in different formats.

**Parameters:**
- `scan_result` (dict, required): Result from `scan_latest_nightly` or `scan_build`
- `format` (str, default: `"daily_findings"`): `"daily_findings"` or `"standup"`

**Example:**
```python
standup = await ciwatch.render(result, format="standup")
```

#### 4. `ciwatch.test_history`

Track a specific test's outcome history across recent builds on main branch (commit-level granularity) to identify regressions, flakes, and failure patterns. Pinpoint the exact commit where a test started failing.

**Parameters:**
- `test_nodeid` (str, required): Full pytest nodeid (e.g., `"tests/test_foo.py::test_bar"`)
- `branch` (str, default: `"main"`): Git branch to scan
- `pipeline` (str, default: `"vllm/ci"`): Buildkite pipeline
- `build_query` (str, optional): Message filter (e.g., `"nightly"`). Default: None (all builds)
- `lookback_builds` (int, default: `50`): Number of recent builds to scan
- `job_filter` (str, optional): Job name filter (e.g., `"Distributed Tests"`)
- `include_logs` (bool, default: `true`): Include log excerpts in output

**Returns:**
```json
{
  "test_nodeid": "tests/test_foo.py::test_bar",
  "timeline": [
    {
      "build_number": 12345,
      "build_url": "https://buildkite.com/...",
      "created_at": "2024-01-22T10:00:00Z",
      "commit_sha": "abc123",
      "test_found": true,
      "test_status": "fail",
      "jobs": [
        {
          "job_name": "Distributed Tests (H100)",
          "job_url": "https://...",
          "status": "fail",
          "fingerprint_normalized": "AssertionError: accuracy <NUM> < <NUM>",
          "log_excerpt": "..."
        }
      ]
    }
  ],
  "assessment": {
    "classification": "REGRESSION",
    "confidence": "HIGH",
    "notes": [
      "Clear transition at build 12344 (commit abc123)",
      "Consistent failure fingerprint across 5 builds"
    ],
    "transition_build": 12344
  },
  "summary": "## Test History: `tests/test_foo.py::test_bar`..."
}
```

**Examples:**
```python
# Basic usage - track last 50 builds on main (one per commit)
result = await ciwatch.test_history(
    test_nodeid="tests/v1/distributed/test_dbo.py::test_dbo_dp_ep_gsm8k[deepep_low_latency]"
)

# Filter to only nightly builds (less granular but faster)
result = await ciwatch.test_history(
    test_nodeid="tests/test_foo.py::test_bar",
    build_query="nightly",
    lookback_builds=30
)

# Filter to specific job type
result = await ciwatch.test_history(
    test_nodeid="tests/test_foo.py::test_bar",
    job_filter="Distributed Tests",
    lookback_builds=40
)

# Scan fewer builds for faster results
result = await ciwatch.test_history(
    test_nodeid="tests/test_foo.py::test_bar",
    lookback_builds=20,
    include_logs=False  # Skip log excerpts for speed
)
```

**Assessment Classifications:**
- **REGRESSION**: Clear transition from passing to failing with consistent error
- **FLAKE_ONSET**: Recent increase in failure rate (20-80%), alternating outcomes
- **PERSISTENT_FAIL**: Failing in >80% of recent builds
- **SPORADIC**: Rare failures (<20%), mostly passing
- **INSUFFICIENT_DATA**: Test found in <3 builds, not enough data

**Key Features:**
- **Commit-level granularity**: One build per commit on main = precise regression detection
- **Automatic assessment**: Classifies as regression, flake, persistent fail, or sporadic
- **Slack-ready output**: Human-friendly summary with Buildkite links to builds/commits

**Limitations:**
- Log parsing may miss tests if output format is non-standard
- Budget limits prevent exhaustive search (max 20 jobs/build, 200KB logs total)
- Fingerprint normalization is conservative - some variations may not be grouped
- Requires builds exist on main branch (works best with CI on every commit)

## Output Examples

### Daily Findings

```markdown
# Daily Findings - 2026-01-19

## Summary
- **Build**: [47580](https://buildkite.com/vllm/ci/builds/47580)
- **Branch**: main
- **Commit**: `abc12345`
- **Total Jobs**: 50, **Failed**: 5
- **Unique Failures**: 8

## Failures by Category

### NEW_REGRESSION (3 failures)

- **tests/test_async_llm_dp.py::test_load[ray-RequestOutputKind.DELTA]** in `GPU Tests`
  - Error: `AssertionError: expected 5, got 3`
  - Reason: New failure with no known pattern
  - Confidence: 50%
  - Owner: alice@example.com (confidence: 90%)

### INFRA_SUSPECTED (2 failures)
...
```

### Standup Summary

```
Nightly build [47580](https://buildkite.com/vllm/ci/builds/47580) FAILED with 8 unique failures: 3 NEW_REGRESSION, 2 INFRA_SUSPECTED, 3 FLAKY_SUSPECTED. Key NEW_REGRESSION tests: test_load, test_async_engine, test_embedding
```

## Classification Logic

### Priority Order

1. **KNOWN_TRACKED**: GitHub issue exists for the test
2. **INFRA_SUSPECTED**: Log matches infrastructure patterns:
   - Timeouts, network errors
   - Out of memory (OOM, CUDA OOM)
   - Disk space issues
   - Process killed (SIGKILL)
3. **FLAKY_SUSPECTED**: Flaky indicators detected:
   - "flaky" in test name
   - "passed on retry" in logs
4. **NEW_REGRESSION**: Has error details but no known pattern
5. **NEEDS_HUMAN_TRIAGE**: Insufficient data

### Deduplication

Failures are deduplicated using a stable hash of:
- Job name (normalized)
- Test name
- Error signature (exception type + first line)

## Architecture

### Modules

- `models.py`: Pydantic schemas for build/job/failure data
- `config.py`: Constants and defaults
- `cli.py`: Subprocess wrappers for `bk`, `gh`, `git`
- `normalize.py`: Pytest log parsing and deduplication
- `classify.py`: Classification heuristics
- `owners.py`: CODEOWNERS parsing and git blame
- `render.py`: Markdown output formatters
- `test_history.py`: Test history tracking orchestration
- `fingerprint.py`: Failure fingerprint normalization
- `assessment.py`: Test history pattern classification
- `server.py`: FastMCP tool registrations

### Data Flow

```
Buildkite API (via bk CLI) → Parse builds/jobs → Fetch logs →
Extract test failures → Classify → Deduplicate → Render markdown
```

## Testing

Run unit tests:

```bash
pytest tests/
```

Run with coverage:

```bash
pytest --cov=ciwatch_mcp tests/
```

Format code:

```bash
black ciwatch_mcp/ tests/
```

## Troubleshooting

### `bk CLI not found`

Install Buildkite CLI:
```bash
brew install buildkite/buildkite/bk
```

### `gh CLI not found`

The server will work without GitHub CLI, but won't match known issues. To enable:
```bash
brew install gh
gh auth login
```

### Empty results

Check that:
1. `BUILDKITE_TOKEN` is set
2. Pipeline slug is correct (default: `vllm/ci`)
3. Branch exists (default: `main`)

### Slow performance

- Fetching logs can be slow for builds with many jobs
- Consider running in background and checking results later
- GitHub issue search adds latency (~1-2s per failure)

## Contributing

Contributions welcome! Please:
1. Add tests for new functionality
2. Format code with `black`
3. Update README for new features

## License

See [LICENSE](LICENSE)
