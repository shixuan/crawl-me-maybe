"""Fetch workers: the contract, and the ways of satisfying it."""

from crawlme.digest.fetcher.base import DEFAULT_UA, Fetcher, FetchError, with_retries
from crawlme.digest.fetcher.browser import PlaywrightFetcher
from crawlme.digest.fetcher.http import HttpFetcher

__all__ = ["DEFAULT_UA", "FetchError", "Fetcher", "HttpFetcher", "PlaywrightFetcher", "with_retries"]
