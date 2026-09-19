"""Phase 3 (and later Phase 4) AI services: DeepSeek client, analysis, rewriting.

Phase 3 implements the analysis half. The rule filter lives in
:mod:`app.services.pipeline.rule_filter` because it is deterministic code, not
model work — and it runs *before* any token is spent.
"""
