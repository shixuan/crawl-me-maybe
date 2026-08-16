"""One lock for every libxml2 entry point in the digest layer.

libxml2 keeps a process-global XML dictionary that concurrent parsers
share, and lxml exposes it without synchronization.  Simultaneous
parses from multiple worker threads intermittently corrupt the heap
and abort the whole process with SIGABRT ("free(): invalid pointer",
"double free or corruption").  Everything that touches lxml here —
trafilatura extraction and BeautifulSoup's "lxml" link harvesting —
takes this lock around the parse, so libxml2 is only ever used by one
thread at a time.

Seen in the wild: three core dumps over two days, all aborts inside
lxml/etree (xmlDictLookup and free paths) during concurrent page
extraction.  Parsing is serialized by this lock; the fetch and LLM
stages keep their own concurrency, so the crawl remains network-bound
and the cost is a few hundred milliseconds of parse time per page.
"""

import threading

LXML_LOCK = threading.Lock()
