from typing import Dict, Any, List, Tuple


# ==========================================================
# CONFIGURABLE QUALITY RULES
# ==========================================================

CRITICAL_FIELDS = [
    "identitas.nama",
    "identitas.npsn",
    "identitas.bentuk_pendidikan",
    "lokasi.kabupaten",
    "lokasi.provinsi",
]

IMPORTANT_SECTIONS = [
    "identitas",
    "lokasi",
    "tenaga_pendidik",
    "peserta_didik",
]


# ==========================================================
# CORE UTILITIES
# ==========================================================

def _is_filled(value: Any) -> bool:
    if value in (None, "", "null", "-", 0):
        return False
    return True


def _flatten_dict(data: Dict[str, Any], parent_key: str = "") -> Dict[str, Any]:
    """
    Flatten nested dictionary:
    { "a": { "b": 1 }} -> { "a.b": 1 }
    """
    items = {}

    for k, v in data.items():
        new_key = f"{parent_key}.{k}" if parent_key else k

        if isinstance(v, dict):
            items.update(_flatten_dict(v, new_key))
        else:
            items[new_key] = v

    return items


# ==========================================================
# MAIN ANALYZER
# ==========================================================

def analyze_profile_quality(profile: Dict[str, Any]) -> Dict[str, Any]:

    flat_profile = _flatten_dict(profile)

    total_fields = len(flat_profile)
    filled_fields = 0
    missing_fields: List[str] = []

    for key, value in flat_profile.items():
        if _is_filled(value):
            filled_fields += 1
        else:
            missing_fields.append(key)

    completeness_score = round(
        filled_fields / total_fields, 2
    ) if total_fields > 0 else 0.0

    # ======================================================
    # CRITICAL FIELD CHECK
    # ======================================================

    critical_missing = [
        field for field in CRITICAL_FIELDS
        if field not in flat_profile or not _is_filled(flat_profile.get(field))
    ]

    # ======================================================
    # SECTION ANALYSIS
    # ======================================================

    section_scores = {}
    weak_sections = []

    for section in IMPORTANT_SECTIONS:
        section_fields = {
            k: v for k, v in flat_profile.items()
            if k.startswith(section + ".")
        }

        if not section_fields:
            section_scores[section] = 0.0
            weak_sections.append(section)
            continue

        total = len(section_fields)
        filled = sum(1 for v in section_fields.values() if _is_filled(v))

        score = round(filled / total, 2)
        section_scores[section] = score

        if score < 0.5:
            weak_sections.append(section)

    # ======================================================
    # QUALITY LABELING
    # ======================================================

    if completeness_score >= 0.9 and not critical_missing:
        quality_level = "EXCELLENT"
    elif completeness_score >= 0.75:
        quality_level = "GOOD"
    elif completeness_score >= 0.5:
        quality_level = "FAIR"
    else:
        quality_level = "POOR"

    # ======================================================
    # CONFIDENCE SCORE (SMARTER METRIC)
    # ======================================================

    confidence_score = round(
        (
            completeness_score * 0.7 +
            (1 - len(critical_missing) / max(len(CRITICAL_FIELDS), 1)) * 0.3
        ),
        2
    )

    return {
        "completeness_score": completeness_score,
        "confidence_score": confidence_score,
        "quality_level": quality_level,
        "total_fields": total_fields,
        "filled_fields": filled_fields,
        "missing_fields": missing_fields,
        "critical_missing_fields": critical_missing,
        "weak_sections": weak_sections,
        "section_scores": section_scores,
    }
