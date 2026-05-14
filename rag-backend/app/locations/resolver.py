"""
app/locations/resolver.py
==========================
Phase 3 target: Smart location resolver menggantikan logika di entity_service.py.

Saat ini: PLACEHOLDER — belum aktif.
Aktif di: Phase 3 (Extract Locations)

Rencana isi:
- LocationResolver class
- Query ke idx_lokasi ES index (dedicated location index)
- Scoring eksplisit: text_similarity + level_specificity + popularity + parent_context
- Decision: AUTO_RESOLVED / RESOLVED_LOW_CONFIDENCE / AMBIGUOUS / NOT_FOUND
- entity_service.py akan jadi adapter yang delegate ke sini
"""
# TODO Phase 3: implement LocationResolver
