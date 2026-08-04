# whoosh-compat

Whoosh query-language parser emitting programmatic tantivy queries.

## Installation

```bash
pip install whoosh-compat
```

## Usage (Planned API)

```python
from whoosh_compat import parse_query

# Parse a Whoosh query string and emit tantivy equivalents
result = parse_query("author:john AND date:[2020 TO 2025]")
print(result.tantivy_query)
```

## Development

Install with dev dependencies:
```bash
pip install -e . --group dev
```
