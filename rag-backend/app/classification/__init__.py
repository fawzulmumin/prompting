"""
app/classification
==================
Multi-tier intent classification system.

Tiers:
  1. PatternClassifier  -- regex/keyword, <5ms
  2. RuleClassifier     -- scoring rules, <20ms
  3. LLMClassifier      -- cognitive_router (LLM), 5-65s

Entry point utama:
    from app.classification import classify_intent, classify_intent_sync
"""
from app.classification.intent_classifier import (
    IntentClassifier,
    ClassificationResult,
    classify_intent,
    classify_intent_sync,
)

__all__ = [
    "IntentClassifier",
    "ClassificationResult",
    "classify_intent",
    "classify_intent_sync",
]
