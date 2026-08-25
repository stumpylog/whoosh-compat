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
# Joining an unquoted multi-word date keyword phrase (DateParserPlugin's
# do_date_phrases) needs the field names already assigned (100), and needs
# the phrase's words still to be separate unfielded word nodes with the
# whitespace between them intact: before MultifieldPlugin (110) rewrites an
# unfielded node into a group, and well before whitespace removal (500).
FILTER_DATE_PHRASES = 101
# 102 runs after the keyword-phrase join (101), so the six two-word keyword
# phrases are already one value node and are never seen as an unquoted
# multi-word run, and before comma values (105) and multifield (110), so
# sibling nodes are still bare unfielded WordNodes.
FILTER_UNQUOTED_DATE_VALUES = 102
FILTER_COMMA_VALUES = 105
FILTER_MULTIFIELD = 110
FILTER_DATES = 110
FILTER_BOOSTS_PRE = 0
FILTER_WHITESPACE_REMOVE = 500
FILTER_BOOSTS_POST = 510
FILTER_OPERATORS = 600
