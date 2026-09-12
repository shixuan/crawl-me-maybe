"""Serialize digest-layer libxml2 access across extraction worker threads.

Concurrent parsing has caused native crashes in this pipeline. Keep extraction
and link parsing under the same lock; fetching and LLM calls remain concurrent."""

import threading

LXML_LOCK = threading.Lock()
