"""Concrete Geocoder implementations for `enrichment/tasks/geocode.py`.

Kept in a separate subpackage so third-party matchers (rapidfuzz,
faiss, requests-based external geocoders) stay optional runtime
dependencies. The base package's `NoOpGeocoder` default requires none
of them. Operators install `.[rag]` to enable the JSON gazetteer.
"""
