"""Stage execution with typed inputs and outputs; stage ordering belongs to the pipelines."""

from crawlme.scheduler.workers.analysis import AnalysisWorker
from crawlme.scheduler.workers.dedup import DedupWorker
from crawlme.scheduler.workers.discovery import DiscoveryWorker
from crawlme.scheduler.workers.fetch import FetchFailure, FetchWorker
from crawlme.scheduler.workers.persist import PersistWorker
from crawlme.scheduler.workers.ranking import RankingWorker

__all__ = [
    "AnalysisWorker",
    "DedupWorker",
    "DiscoveryWorker",
    "FetchFailure",
    "FetchWorker",
    "PersistWorker",
    "RankingWorker",
]
