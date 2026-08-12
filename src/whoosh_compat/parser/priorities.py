"""Priority constants for tagger and filter ordering in the query parser.

These are not forked from whoosh (whoosh inlines the numeric priorities as
plugin attributes/comments); this module centralizes them so parser plugins
can share a single source of truth.
"""

# Tagger priorities
TAG_WHITESPACE = 100
TAG_RANGE = 1
TAG_EVERY = -1
TAG_DEFAULT = 0
TAG_OPERATOR = 0
TAG_OPERATOR_ANDNOT = -5

# Filter priorities
FILTER_GROUPS = 0
FILTER_WILDCARDS = 50
FILTER_ALIASES = 90
FILTER_GTLT = 99
FILTER_FIELDNAMES = 100
FILTER_COMMA_VALUES = 105
FILTER_MULTIFIELD = 110
FILTER_DATES = 110
FILTER_BOOSTS_PRE = 0
FILTER_WHITESPACE_REMOVE = 500
FILTER_BOOSTS_POST = 510
FILTER_OPERATORS = 600
