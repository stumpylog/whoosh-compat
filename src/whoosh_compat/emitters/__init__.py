"""Backend query emitters for whoosh-compat.

This package intentionally does NOT import any concrete backend (e.g.
``tantivy``) at module import time — backends are optional dependencies.
Import the specific emitter module you need directly, e.g.::

    from whoosh_compat.emitters.tantivy_ import emit
"""
