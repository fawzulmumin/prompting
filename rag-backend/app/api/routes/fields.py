from fastapi import APIRouter
from app.domain.constants.field_catalog import FIELD_CATALOG, _LOW_COVERAGE_FIELDS

router = APIRouter()


@router.get("/fields")
async def list_fields(include_low_coverage: bool = False):
    """Kembalikan catalog field yang tersedia untuk query filter dan distribusi."""
    result = {
        name: {
            "label": info["label"],
            "values": info["values"],
            "distribusi": info.get("distribusi", False),
            "type": info.get("type", "string"),
            "index": info.get("index", "both"),
            "notes": info.get("notes", ""),
        }
        for name, info in FIELD_CATALOG.items()
    }
    if include_low_coverage:
        result["_low_coverage"] = _LOW_COVERAGE_FIELDS
    return {"fields": result}
