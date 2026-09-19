"""Stage execution with typed inputs and outputs; scheduling belongs to the engine."""

from crawlme.scheduler.workers.analysis import AnalysisWorker
from crawlme.scheduler.workers.discovery import DiscoveryWorker
from crawlme.scheduler.workers.fetch import FetchFailure, FetchWorker
from crawlme.scheduler.workers.persist import PersistWorker
from crawlme.scheduler.workers.ranking import RankingWorker

__all__ = ["AnalysisWorker", "DiscoveryWorker", "FetchFailure", "FetchWorker", "PersistWorker", "RankingWorker"]
