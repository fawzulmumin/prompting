"""
app/narrators/base.py
======================
Phase 5 target: BaseNarrator ABC dan shared contracts.

Saat ini: PLACEHOLDER — belum aktif.
Aktif di: Phase 5 (Refactor Narrator)

Rencana isi:
- BaseNarrator(ABC) dengan method narrate() abstract
- NarrationRequest dataclass: persona, detail, streaming, user_goal
- NarrationOutput dataclass: text, used_llm, fallback_used, validation_passed, duration_ms
- Shared helpers: validate_numbers(), call_llm(), fallback_to_template()
- narrator_service.py akan di-pecah jadi implementasi per-intent yang extend BaseNarrator
"""
# TODO Phase 5: implement BaseNarrator
