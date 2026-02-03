# Testing Guide: CI Watch Workflow

## Summary

The new tools implement two workflows depending on what the scan returns:

### Workflow A: Scan has pytest nodeids (best case)
```
scan_latest_nightly() → filter for "::" → get_test_analytics_bulk() → report
```

### Workflow B: Scan only has job-level failures (infrastructure issues)
```
scan_latest_nightly() → no "::" found → report as infrastructure failures
```

The updated docstrings now guide Claude to check for `::` in test names to differentiate pytest tests from job-level failures.

---

## Expected Behavior

### When scan returns pytest test failures:
```python
scan = scan_latest_nightly()
# failures contain: "tests/foo.py::test_bar"  ← has "::"

pytest_failures = [f for f in scan["failures"]
                   if "::" in f["test_failure"]["test_name"]]

test_nodeids = [f["test_failure"]["test_name"] for f in pytest_failures]
analytics = get_test_analytics_bulk(test_nodeids)

# Report: X flaky, Y new regressions
```

### When scan returns only job-level failures:
```python
scan = scan_latest_nightly()
# failures contain: "Language Models Test (Extended Pooling)"  ← no "::"

pytest_failures = [f for f in scan["failures"]
                   if "::" in f["test_failure"]["test_name"]]
# → Empty list!

# Report: These are infrastructure failures (docker builds, setup failures, etc.)
#         Not pytest test regressions - different investigation needed
```

---

## Test Prompts

### Test 1: Generic CI Watch (Should adapt to what's found)
```
I'm on CI watch for vLLM. Check the latest nightly and let me know what needs attention.
For test failures, classify them as flaky vs real issues. For infrastructure failures,
just summarize what failed.
```

**Expected flow:**
1. `scan_latest_nightly()`
2. Check if failures have `"::"` in test_name
3. **If yes**: Extract nodeids → `get_test_analytics_bulk()` → classify flaky vs new
4. **If no**: Report as infrastructure failures

### Test 2: Specific build analysis
```
Check build 48161 and tell me which test failures are known flaky vs new regressions.
```

**Expected flow:**
1. `scan_build("48161")`
2. Filter for pytest failures (`"::"`)
3. If found: `get_test_analytics_bulk()` → classify
4. If not: Report infrastructure issues

---

## What to Watch For

### ✅ Good behavior:
- Claude checks for `"::"` to identify pytest tests
- Uses `get_test_analytics_bulk()` only when there are pytest nodeids
- Reports infrastructure failures separately (docker builds, etc.)
- Doesn't call `get_job_test_failures()` unnecessarily

### ❌ Bad behavior (what we fixed):
- Calling `get_job_test_failures()` on infrastructure jobs (docker builds)
- Trying to send job names to `get_test_analytics_bulk()`
- Not recognizing the difference between pytest tests and job-level failures

---

## Key Improvements Made

1. **Docstring guidance**: Added explicit workflow steps with `::` check
2. **Fuzzy matching**: Now prefers exact matches when multiple jobs match
3. **Better error messages**: Returns helpful candidates list
4. **Workflow hints**: Shows the exact code pattern to follow

---

## Manual Testing

Run the MCP server and use one of the test prompts above. Watch for:

1. Does Claude check for `"::"` in test names?
2. Does it handle infrastructure failures appropriately?
3. Does it use `get_test_analytics_bulk()` correctly?
4. Does the final report make sense?

---

## Example Good Output

```
Checked build #49718. Found 10 failures:

Infrastructure failures (not test regressions):
  • :docker: Build CPU arm64 image
  • Distributed Tests (2 GPUs)(H100) - environment setup failed
  • Build torch nightly image - dependency issue

These are infrastructure/environment issues, not flaky test problems.
Recommend checking Docker build logs and environment configuration.
```

vs

```
Checked build #48123. Found 15 test failures:

Pytest test failures analyzed:
  • 3 known flaky tests (can ignore):
    - tests/entrypoints/test_vision.py::test_single_chat[...]
    - tests/models/test_llm.py::test_async[...]
  • 2 new regressions (investigate!):
    - tests/core/test_engine.py::test_scheduler
    - tests/worker/test_gpu.py::test_memory_allocation

Infrastructure failures:
  • Docker build failed (unrelated to tests)

Action needed: Investigate the 2 new test regressions.
```
