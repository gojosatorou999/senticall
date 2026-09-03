"""Concrete LLMExtractor implementations for `enrichment/tasks/extract.py`.

Kept in a separate subpackage so third-party client libraries (anthropic,
openai, boto3-bedrock) are optional runtime dependencies — the base
package's `NoOpExtractor` default requires none of them. Operators
install `.[llm]` to enable the real extractors.
"""
