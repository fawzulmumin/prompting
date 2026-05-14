"""
Shared Pydantic models untuk /ask dan /ask/stream endpoints.
Dipindahkan dari main.py agar bisa diimport dari stream.py tanpa circular import.
"""

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    user_id: Optional[str] = None
    level: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_km: Optional[float] = None

    @field_validator("question")
    @classmethod
    def strip_question(cls, v: str) -> str:
        return v.strip()


class AskResponse(BaseModel):
    query: str
    answer_markdown: str
    answer_html: str
    provider: str
    from_cache: bool
    duration_ms: int
    context_used: List[str]
    context_sufficiency_score: float
    answer_confident: bool
    confidence_score: Optional[float] = None
    completeness_score: Optional[float] = None
    user_id: str
    num_docs: int
    suggestions: Optional[List[str]] = None
