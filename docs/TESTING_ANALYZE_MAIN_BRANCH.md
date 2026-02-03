# Testing `ciwatch.analyze_main_branch`

## Manual Testing Guide

### Prerequisites
- MCP server running: `uv run ciwatch-mcp` or `python -m ciwatch_mcp.server`
- MCP Inspector or Claude Code connected
- BUILDKITE_TOKEN environment variable set

### Test Cases

#### 1. Basic Invocation (Default Parameters)
```json
{
  "pipeline": "vllm/ci",
  "branch": "main"
}
```

**Expected:**
- Returns analysis of last 24 hours
- Scans up to 5 failed builds
- Excludes scheduled/nightly builds
- Shows recurrence data for each failure

**Verify:**
- `analysis_window.hours_lookback` = 24
- `analysis_window.builds_scanned` ≤ 5
- `builds_analyzed` list does not contain builds with `source: "schedule"`
- Each failure has `occurrence_count`, `seen_in_builds`, `seen_in_commits`, `recurrence_rate`

#### 2. Custom Time Window
```json
{
  "pipeline": "vllm/ci",
  "branch": "main",
  "hours_lookback": 48,
  "max_builds": 10
}
```

**Expected:**
- Scans last 48 hours
- Processes up to 10 builds

**Verify:**
- `analysis_window.hours_lookback` = 48
- `analysis_window.builds_scanned` ≤ 10

#### 3. Include Scheduled Builds
```json
{
  "pipeline": "vllm/ci",
  "branch": "main",
  "exclude_scheduled": false
}
```

**Expected:**
- Includes nightly builds in analysis

**Verify:**
- `builds_analyzed` may include builds with `source: "schedule"` (if any failed in window)

#### 4. Detail Levels

**Minimal:**
```json
{
  "detail_level": "minimal"
}
```
**Verify:** Failures have no `error_message`, `stack_trace`, `log_snippet`

**Summary:**
```json
{
  "detail_level": "summary"
}
```
**Verify:** Failures have `error_message`, truncated `log_snippet`, no `stack_trace`

**Full:**
```json
{
  "detail_level": "full"
}
```
**Verify:**
- All failure details present
- `analysis_text` field exists with formatted summary

#### 5. Integration Workflow

```python
# Step 1: Analyze main branch
result = analyze_main_branch(hours_lookback=24, max_builds=5)

# Step 2: Extract pytest nodeids
pytest_failures = [
    f for f in result["failures"]
    if "::" in f["test_failure"]["test_name"]
]
test_nodeids = [f["test_failure"]["test_name"] for f in pytest_failures]

# Step 3: Check if tests are flaky (use get_test_analytics_bulk)
# (This would be a separate MCP call)

# Step 4: Identify patterns
persistent = [f for f in pytest_failures if f["recurrence_rate"] >= 0.5]
intermittent = [f for f in pytest_failures if f["recurrence_rate"] < 0.5]
```

**Verify:**
- Can extract nodeids from failures
- `recurrence_rate` correctly identifies persistent vs intermittent failures
- Summary stats match actual counts

### Edge Cases

#### No Failed Builds in Window
```json
{
  "hours_lookback": 1
}
```
**Expected:** Error message indicating no failed builds found

#### No Builds at All
```json
{
  "branch": "nonexistent-branch"
}
```
**Expected:** Error message indicating no builds found

#### All Builds Passed
**Expected:** Error message indicating no failed builds found (after filtering)

### Validation Checklist

- [ ] Tool can be invoked via MCP
- [ ] Default parameters work correctly
- [ ] Time window filtering works (hours_lookback)
- [ ] Build limit works (max_builds)
- [ ] Scheduled builds excluded by default
- [ ] Recurrence tracking works (multiple builds with same failure)
- [ ] Deduplication works (same failure in multiple builds)
- [ ] Detail level filtering works (minimal/summary/full)
- [ ] Summary stats accurate (persistent vs intermittent counts)
- [ ] Error handling works (no builds, API errors)
- [ ] GitHub issue matching works (if search_github=true)
- [ ] Owner inference works (if VLLM_REPO_PATH set)

### Performance Benchmarks

Expected timing (approximate):
- 1 build: ~5-10 seconds
- 5 builds: ~15-30 seconds
- 10 builds: ~30-60 seconds

Factors affecting performance:
- Number of failed jobs per build
- Log size per job
- GitHub API response time (if search_github=true)
- Network latency to Buildkite API

### Example Output Structure

```json
{
  "analysis_window": {
    "start_time": "2026-02-02T12:00:00Z",
    "end_time": "2026-02-03T12:00:00Z",
    "hours_lookback": 24,
    "builds_scanned": 5,
    "builds_analyzed": [
      {
        "build_number": "48200",
        "commit": "abc123",
        "state": "failed",
        "created_at": "2026-02-03T10:30:00Z"
      }
    ]
  },
  "summary": {
    "total_builds_scanned": 5,
    "builds_with_failures": 3,
    "total_unique_failures": 12,
    "persistent_failures": 4,
    "intermittent_failures": 8
  },
  "failures": [
    {
      "failure_key": "abc123def456",
      "test_failure": {
        "test_name": "tests/models/test_llm.py::test_async_engine",
        "job_name": "GPU Tests (H100)",
        "error_message": "AssertionError: ...",
        "stack_trace": null,
        "log_snippet": "..."
      },
      "category": "NEW_REGRESSION",
      "github_issue": null,
      "confidence": 0.5,
      "reason": "New failure with no known pattern",
      "owner": null,
      "owner_confidence": null,
      "occurrence_count": 3,
      "seen_in_builds": ["48200", "48198", "48195"],
      "seen_in_commits": ["abc123", "def456", "ghi789"],
      "recurrence_rate": 0.6
    }
  ],
  "scan_timestamp": "2026-02-03T12:00:00Z"
}
```

### Common Issues

**Issue:** No builds found
**Solution:** Check branch name, increase hours_lookback, or check if any builds exist in the time window

**Issue:** All builds passing (no failures returned)
**Solution:** This is expected if main is healthy. Try increasing max_builds or hours_lookback

**Issue:** Slow performance
**Solution:** Reduce max_builds, disable GitHub search, or use minimal detail level

**Issue:** Missing recurrence data
**Solution:** Verify failures are actually appearing in multiple builds - may be legitimately unique to one build
