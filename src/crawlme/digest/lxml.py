"""One lock for every libxml2 entry point in the digest layer.

libxml2 keeps a process-global XML dictionary that concurrent parsers
share, and lxml exposes it without synchronization.  Simultaneous
parses from several worker threads intermittently corrupt the heap and
abort the whole process with SIGABRT.  Three core dumps over two days,
all inside lxml/etree during concurrent extraction.

Everything here that touches lxml, trafilatura extraction and
BeautifulSoup link harvesting, takes this lock around the parse.  Only
parsing is serialized; fetch and the model calls keep their own
concurrency, so the crawl stays network-bound.
"""

import threading

LXML_LOCK = threading.Lock()
