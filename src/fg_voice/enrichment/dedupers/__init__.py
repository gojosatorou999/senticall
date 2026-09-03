"""Concrete DedupeStrategy implementations for
`enrichment/tasks/dedupe.py`.

Kept in a separate subpackage so external matchers (rapidfuzz today,
FAISS + PostGIS ST_DWithin later in P4/P7) stay optional runtime
dependencies. The base package's `NoDedupeStrategy` default requires
none of them.
"""
