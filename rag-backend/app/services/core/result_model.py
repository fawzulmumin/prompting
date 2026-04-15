# result_model.py

from enum import Enum
from typing import Optional, Dict, Any


class ResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


def build_result(
    status: ResultStatus,
    provider: str = "SYSTEM",
    data: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    confidence: Optional[float] = None,
):
    return {
        "status": status,
        "provider": provider,
        "data": data or {},
        "reason": reason,
        "metadata": metadata or {},
        "confidence": confidence,
    }
