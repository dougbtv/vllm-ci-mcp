"""Buildkite REST API client using requests."""

import os
from typing import Optional

import requests


class BuildkiteAPIError(Exception):
    """Raised when Buildkite API request fails."""

    pass


class BuildkiteClient:
    """Client for Buildkite REST API.

    Handles authentication and requests to both the main API and Test Analytics API.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        org_slug: str = "vllm",
        timeout: int = 30,
        log_timeout: int = 60,
    ):
        """Initialize Buildkite API client.

        Args:
            token: Buildkite API token (defaults to BUILDKITE_TOKEN or BUILDKITE_API_TOKEN env var)
            org_slug: Organization slug (defaults to "vllm")
            timeout: Default request timeout in seconds
            log_timeout: Timeout for log requests in seconds
        """
        self.token = token or os.environ.get("BUILDKITE_TOKEN") or os.environ.get("BUILDKITE_API_TOKEN")
        if not self.token:
            raise BuildkiteAPIError(
                "BUILDKITE_TOKEN or BUILDKITE_API_TOKEN environment variable not set"
            )

        # Allow override via env var
        self.org_slug = os.environ.get("BUILDKITE_ORG", org_slug)

        self.base_url = "https://api.buildkite.com/v2"
        self.analytics_base_url = "https://api.buildkite.com/v2/analytics"

        self.timeout = timeout
        self.log_timeout = log_timeout

        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _parse_pipeline(self, pipeline: str) -> tuple[str, str]:
        """Parse pipeline slug into org and pipeline name.

        Args:
            pipeline: Pipeline slug (e.g., "vllm/ci" or "ci")

        Returns:
            Tuple of (org_slug, pipeline_slug)
        """
        if "/" in pipeline:
            org, pipe = pipeline.split("/", 1)
            return org, pipe
        else:
            # Use default org if no org in pipeline
            return self.org_slug, pipeline

    def _request(
        self,
        method: str,
        url: str,
        timeout: Optional[int] = None,
        **kwargs
    ) -> requests.Response:
        """Make HTTP request with error handling.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Full URL to request
            timeout: Request timeout (defaults to self.timeout)
            **kwargs: Additional args passed to requests

        Returns:
            Response object

        Raises:
            BuildkiteAPIError: On timeout, HTTP error, or connection error
        """
        if timeout is None:
            timeout = self.timeout

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                timeout=timeout,
                **kwargs
            )
            response.raise_for_status()
            return response

        except requests.Timeout:
            raise BuildkiteAPIError(f"Request timed out after {timeout}s: {url}")
        except requests.HTTPError as e:
            raise BuildkiteAPIError(f"HTTP error {e.response.status_code}: {e.response.text}")
        except requests.RequestException as e:
            raise BuildkiteAPIError(f"Request failed: {str(e)}")

    def list_builds(
        self,
        pipeline: str,
        branch: Optional[str] = None,
        limit: int = 30,
        state: Optional[str] = None,
    ) -> list[dict]:
        """Get build list from Buildkite API.

        Args:
            pipeline: Pipeline slug (e.g., "vllm/ci")
            branch: Git branch to filter by
            limit: Number of builds to return (max 100 per page)
            state: Optional state filter (e.g., "failed", "passed")

        Returns:
            List of build dicts

        Raises:
            BuildkiteAPIError: If request fails
        """
        org, pipe = self._parse_pipeline(pipeline)
        url = f"{self.base_url}/organizations/{org}/pipelines/{pipe}/builds"

        params = {"per_page": min(limit, 100)}
        if branch:
            params["branch"] = branch
        if state:
            params["state"] = state

        try:
            response = self._request("GET", url, params=params)
            return response.json()
        except ValueError as e:
            raise BuildkiteAPIError(f"Failed to parse JSON response: {e}")

    def get_build(self, pipeline: str, build_number: str) -> dict:
        """Get detailed build information including all jobs.

        Args:
            pipeline: Pipeline slug
            build_number: Build number

        Returns:
            Build dict with jobs array

        Raises:
            BuildkiteAPIError: If request fails
        """
        org, pipe = self._parse_pipeline(pipeline)
        url = f"{self.base_url}/organizations/{org}/pipelines/{pipe}/builds/{build_number}"

        try:
            response = self._request("GET", url)
            return response.json()
        except ValueError as e:
            raise BuildkiteAPIError(f"Failed to parse JSON response: {e}")

    def get_job_log(
        self,
        pipeline: str,
        build_number: str,
        job_id: str
    ) -> str:
        """Fetch raw log text for a job.

        Args:
            pipeline: Pipeline slug
            build_number: Build number
            job_id: Job ID

        Returns:
            Raw log text (string)

        Raises:
            BuildkiteAPIError: If request fails
        """
        org, pipe = self._parse_pipeline(pipeline)
        url = f"{self.base_url}/organizations/{org}/pipelines/{pipe}/builds/{build_number}/jobs/{job_id}/log"

        # Use longer timeout for log fetches
        response = self._request("GET", url, timeout=self.log_timeout)

        # Log endpoint returns plain text, not JSON
        return response.text

    def list_analytics_tests(
        self,
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
            limit: Max results (max 100 per page)

        Returns:
            List of test dicts from Test Analytics API

        Raises:
            BuildkiteAPIError: If request fails
        """
        url = f"{self.analytics_base_url}/organizations/{self.org_slug}/suites/{suite_slug}/tests"

        params = {"per_page": min(limit, 100)}
        if order:
            params["order"] = order
        if state:
            params["state"] = state

        try:
            response = self._request("GET", url, params=params)
            return response.json()
        except ValueError as e:
            raise BuildkiteAPIError(f"Failed to parse JSON response: {e}")

    def get_analytics_test(self, suite_slug: str, test_id: str) -> dict:
        """Get detailed info for a specific test.

        Args:
            suite_slug: Test suite slug
            test_id: Test ID

        Returns:
            Test detail dict

        Raises:
            BuildkiteAPIError: If request fails
        """
        url = f"{self.analytics_base_url}/organizations/{self.org_slug}/suites/{suite_slug}/tests/{test_id}"

        try:
            response = self._request("GET", url)
            return response.json()
        except ValueError as e:
            raise BuildkiteAPIError(f"Failed to parse JSON response: {e}")

    def get_analytics_test_runs(
        self,
        suite_slug: str,
        test_id: str,
        limit: int = 100,
    ) -> list[dict]:
        """Get run history for a specific test.

        Args:
            suite_slug: Test suite slug
            test_id: Test ID
            limit: Max results (max 100 per page)

        Returns:
            List of run dicts with commit_sha, created_at, status, etc.

        Raises:
            BuildkiteAPIError: If request fails
        """
        url = f"{self.analytics_base_url}/organizations/{self.org_slug}/suites/{suite_slug}/tests/{test_id}/runs"

        params = {"per_page": min(limit, 100)}

        try:
            response = self._request("GET", url, params=params)
            return response.json()
        except ValueError as e:
            raise BuildkiteAPIError(f"Failed to parse JSON response: {e}")
