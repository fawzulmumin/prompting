"""
Data models untuk intent classification.
Dipindah dari cognitive_router.py (P17-B refactor).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum


@dataclass
class WilayahEntities:
    provinsi:   Optional[str] = None
    kabupaten:  Optional[str] = None
    kecamatan:  Optional[str] = None
    desa:       Optional[str] = None
    landmark:   Optional[str] = None
    ambiguous:  bool = False
    candidates: list = field(default_factory=list)


@dataclass
class CognitiveIntent:
    # Routing
    module:      str = "out_of_context"
    secondary:   List[str] = field(default_factory=list)
    scope:       str = "nasional"
    sub_queries: Dict[str, str] = field(default_factory=dict)

    # Entities
    jenjang:          Optional[str] = None
    jenjang_list:     List[str] = field(default_factory=list)
    status:           Optional[str] = None
    akreditasi:       Optional[str] = None
    akreditasi_mode:  Optional[str] = None
    akreditasi_list:  List[str] = field(default_factory=list)
    nama_sekolah:     Optional[str] = None
    npsn:             Optional[str] = None
    wilayah:          WilayahEntities = field(default_factory=WilayahEntities)
    wilayah_bandingkan: Optional[str] = None
    yayasan:          Optional[str] = None
    koordinat_user:   bool = False
    radius_km:        Optional[float] = None
    metric_target:    Optional[str] = None

    # QC filters
    qc_masalah_only:  bool = False
    qc_field:         Optional[int] = None
    qc_min_masalah:   Optional[int] = None
    sort_by:          Optional[str] = None
    sort_order:       Optional[str] = None
    qc_persen_min:    Optional[float] = None
    qc_persen_max:    Optional[float] = None
    qc_field_list:    List[int] = field(default_factory=list)
    qc_persen_query:  bool = False

    # Atribut sekolah
    keaktifan:          Optional[str]  = None
    wilayah_terpencil:  Optional[bool] = None
    wilayah_perbatasan: Optional[bool] = None

    # OOT
    is_oot:     bool = False
    oot_reason: Optional[str] = None

    # Dependency-aware orchestration
    dependencies: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Debug
    raw_label: Optional[str] = None

    @property
    def primary(self) -> str:
        return self.module

    @primary.setter
    def primary(self, value: str) -> None:
        self.module = value

    @property
    def filters(self) -> dict:
        result = {}
        if self.jenjang:        result["jenjang"]       = self.jenjang
        if self.status:         result["status"]         = self.status
        if self.akreditasi:     result["akreditasi"]     = self.akreditasi
        if self.nama_sekolah:   result["nama_sekolah"]   = self.nama_sekolah
        if self.npsn:           result["npsn"]           = self.npsn
        if self.yayasan:        result["yayasan"]        = self.yayasan
        if self.radius_km is not None:
            result["radius_km"] = self.radius_km
        return result


VALID_MODULES = {
    "aggregation", "comparison", "profile", "list", "geo", "out_of_context"
}

VALID_SCOPES = {
    "nasional", "provinsi", "kabupaten", "kecamatan", "desa", "geo_radius"
}


class QueryRoute(str, Enum):
    AGGREGATION    = "aggregation"
    COMPARISON     = "comparison"
    PROFILE        = "profile"
    LIST           = "list"
    GEO            = "geo"
    OUT_OF_CONTEXT = "out_of_context"
