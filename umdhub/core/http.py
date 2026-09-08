"""Shared HTTP client: pooled session, timeouts, retry on transient errors.
Trimmed from Hardware Parser core/http.py — no UA rotation or token bucket; every
source here talks to one host a handful of times per run."""
from __future__ import annotations

import logging
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

log = logging.getLogger(__name__)

UA = "umd-hub/0.1 (+personal deadline aggregator; contact: owner)"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = exc.response
        return resp is None or resp.status_code == 429 or resp.status_code >= 500
    return False


class HttpClient:
    def __init__(self, user_agent: str = UA):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    @retry(retry=retry_if_exception(_is_retryable), stop=stop_after_attempt(3),
           wait=wait_exponential_jitter(initial=1, max=15), reraise=True)
    def request(self, method: str, url: str, *, timeout: float = 25.0,
                allow_redirects: bool = True, **kwargs: Any) -> requests.Response:
        resp = self.session.request(method, url, timeout=timeout, allow_redirects=allow_redirects, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            log.warning("http %s on %s; will retry", resp.status_code, url)
            resp.raise_for_status()
        return resp

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)
