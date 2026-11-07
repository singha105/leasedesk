"""sentinel's analyzers. Each is ``analyze(proposal, context) -> list[Finding]`` and pure,
except ``aws`` (Access Analyzer calls, real mode only), which ``review.py`` runs separately.
``review.py`` holds the ordered list of analyzers."""
