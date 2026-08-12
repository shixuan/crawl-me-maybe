# How to read?

Each run creates a timestamped directory: `results/YYYYMMDD_HHMMSS/`

## Directory structure

```
20250101_120000/
├── db/crawl.db    # SQLite database with all crawl data
└── raw/           # raw HTML files (content-addressed)
```

## How to inspect

### Pages fetched

```bash
sqlite3 -readonly results/<run>/db/crawl.db \
  "SELECT title, json_extract(url_json, '$.raw') FROM pages ORDER BY rowid;"
```

### Candidates that passed pre-filter (BUFFERED)

```bash
sqlite3 -readonly results/<run>/db/crawl.db \
  "SELECT anchor, depth, json_extract(url_json, '$.raw') FROM candidates WHERE status='BUFFERED' LIMIT 20;"
```

### Candidates filtered out

```bash
sqlite3 -readonly results/<run>/db/crawl.db \
  "SELECT status, COUNT(*) FROM candidates GROUP BY status;"
```

### Rank decisions

```bash
sqlite3 -readonly results/<run>/db/crawl.db \
  "SELECT priority, dropped, ranker, rationale FROM rank_decisions LIMIT 20;"
```

### All tables

```bash
sqlite3 -readonly results/<run>/db/crawl.db ".tables"
```

## Cleanup

```bash
rm -rf results/202*
```
