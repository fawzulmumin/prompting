# profile_search_service.py

import re
import logging
import json
from app.config import ELASTICSEARCH_INDEX

logger = logging.getLogger(__name__)
 
 
class ProfileSearchService:
 
    def __init__(self, es_client):
        self.es = es_client
        self.index = ELASTICSEARCH_INDEX
 
    # =====================================================
    # SEARCH BY NPSN
    # =====================================================
    async def search_by_npsn(self, npsn: str, year: int | None = None) -> dict:
 
        filters = [{"term": {"npsn": npsn}}]
 
        if year:
            filters.append({"term": {"tahun": year}})
 
        query = {
            "query": {
                "bool": {
                    "filter": filters
                }
            },
            "sort": [
                {"tahun": {"order": "desc"}}
            ]
        }
 
        return await self._execute(query)
 
    # =====================================================
    # SEARCH BY SCHOOL NAME
    # =====================================================
    async def search_by_name(
        self,
        school_name: str,
        bentuk: str | None = None,
        wilayah: str | None = None,
        year: int | None = None,
        number: str | None = None,
        strict_wilayah: bool = False
    ):
 
        school_name = self._normalize_text(school_name)
        school_name = self._strip_intent_words(school_name)
 
        wilayah_clean = None
        if wilayah:
            wilayah_clean = self._strip_location_prefix(
                self._normalize_text(wilayah)
            )
 
        query = {
            "size": 20,
            "track_total_hits": True,
            "query": {
                "bool": {
                    "should": self._build_name_queries(school_name),
                    "filter": self._build_filters(bentuk, year),
                    "minimum_should_match": 1
                }
            },
            "collapse": {"field": "npsn"},
            "sort": [{"_score": {"order": "desc"}}]
        }
 
        # =============================================
        # LOCATION BOOST — always add as should clause
        # =============================================
        if wilayah_clean:
            query["query"]["bool"]["should"].extend(
                self._build_location_boost(wilayah_clean)
            )
 
        # =============================================
        # STRICT WILAYAH MODE
        # Tambahkan filter wilayah agar ES hanya
        # mengembalikan sekolah dari daerah yang dimaksud.
        # Digunakan saat ada nomor sekolah + wilayah spesifik.
        # Tanpa ini, "SMA Negeri 4 Tangerang Selatan"
        # tidak masuk top 20 karena kalah ES score.
        # =============================================
        if strict_wilayah and wilayah_clean:
            query["query"]["bool"]["filter"].append(
                self._build_wilayah_filter(wilayah_clean)
            )
        logger.debug("[PROFILE] nama_sekolah input: %s", school_name)
        logger.debug("[PROFILE] wilayah input: %s", wilayah_clean)
        logger.debug("[PROFILE] ES query: %s", json.dumps(query, indent=2))
        return await self._execute(query)
 
    # =====================================================
    # BUILD NAME QUERIES
    # =====================================================
    def _build_name_queries(self, school_name):
 
        queries = []
 
        queries.append({
            "term": {
                "nama.keyword": {
                    "value": school_name.lower(),
                    "boost": 15
                }
            }
        })
        
        queries.append({
            "match_phrase": {
                "nama": {
                    "query": school_name,
                    "slop": 2,
                    "boost": 12
                }
            }
        })
        queries.append({
            "multi_match": {
                "query": school_name,
                "fields": ["nama^6", "nama.keyword^12"],
                "fuzziness": "1",
                "boost": 6
            }
        })
        queries.append({
            "multi_match": {
                "query": school_name,
                "fields": [
                    "kabupaten.text^1",
                    "kecamatan.text^0.5",
                    "content"
                ],
                "type": "best_fields",
                "fuzziness": "AUTO",
                "boost": 2
            }
        })
 
        number_match = re.search(r"\b\d+\b", school_name)
        if number_match:
            number = number_match.group(0)
            queries.append({
                "match_phrase": {
                    "nama": {
                        "query": school_name,
                        "boost": 12
                    }
                }
            })
            queries.append({
                "match_phrase": {
                    "nama": {
                        "query": number,
                        "boost": 8
                    }
                }
            })
            # Juga cari dengan angka yang dipadu-zero (2 → 02, 02 → 2)
            try:
                num_int = int(number)
                alt_number = f"{num_int:02d}" if len(number) == 1 else str(num_int)
                if alt_number != number:
                    queries.append({
                        "match_phrase": {
                            "nama": {
                                "query": alt_number,
                                "boost": 8
                            }
                        }
                    })
            except ValueError:
                pass
 
        queries.append({
            "match": {
                "nama": {
                    "query": school_name,
                    "operator": "and",
                    "minimum_should_match": "70%",
                    "boost": 3
                }
            }
        })
 
        queries.append({
            "match": {
                "nama": {
                    "query": school_name,
                    "fuzziness": "AUTO",
                    "prefix_length": 2,
                    "boost": 1
                }
            }
        })
 
        queries.append({
            "match": {
                "content": {
                    "query": school_name,
                    "fuzziness": "AUTO",
                    "boost": 0.5
                }
            }
        })
 
        # =============================================
        # VARIASI NAMA SEKOLAH
        # ES menyimpan: "SD NEGERI X", "SD N X", "SDN X"
        # Agar semua variasi match, tambahkan should clause
        # =============================================
        variants = self._build_name_variants(school_name)
        for variant in variants:
            queries.append({
                "match_phrase": {
                    "nama": {
                        "query": variant,
                        "slop": 1,
                        "boost": 10
                    }
                }
            })
 
        return queries
 
    def _build_name_variants(self, school_name: str) -> list:
        """
        Generate variasi singkatan nama sekolah.
        "sd negeri 02 parungpanjang" → ["sdn 02 parungpanjang", "sd n 02 parungpanjang"]
        "smp negeri 1"               → ["smpn 1", "smp n 1"]
        """
        variants = []
        ABBREV_MAP = {
            "sd negeri":  ["sdn", "sd n"],
            "smp negeri": ["smpn", "smp n"],
            "sma negeri": ["sman", "sma n"],
            "smk negeri": ["smkn", "smk n"],
            "mts negeri": ["mtsn", "mts n"],
            "ma negeri":  ["man", "ma n"],
        }
        name_lower = school_name.lower()
        for full, abbrevs in ABBREV_MAP.items():
            if full in name_lower:
                rest = name_lower.replace(full, "").strip()
                for abbr in abbrevs:
                    variants.append(f"{abbr} {rest}".strip())
        return variants
    # =====================================================
    # LOCATION BOOST — should clause
    # =====================================================
    def _build_location_boost(self, wilayah):
 
        return [
            {
                "match": {
                    "kabupaten.text": {
                        "query": wilayah,
                        "boost": 15,
                        "fuzziness": "AUTO"
                    }
                }
            },
            {
                "match": {
                    "kecamatan.text": {
                        "query": wilayah,
                        "boost": 8,
                        "fuzziness": "AUTO"
                    }
                }
            },
            {
                "match": {
                    "provinsi.text": {
                        "query": wilayah,
                        "boost": 3,
                        "fuzziness": "AUTO"
                    }
                }
            },
            {
                "term": {
                    "kabupaten": {
                        "value": wilayah.lower(),
                        "boost": 20
                    }
                }
            },
        ]
 
    # =====================================================
    # WILAYAH FILTER — filter clause (strict mode)
    # Gunakan bool should agar match kabupaten ATAU kecamatan
    # =====================================================
    def _build_wilayah_filter(self, wilayah):
        """
        Filter dokumen berdasarkan wilayah.
        Gunakan match_phrase (bukan fuzziness) agar
        "tangerang selatan" tidak false-positive match
        "nias selatan" atau "buton selatan".
        """
        return {
            "bool": {
                "should": [
                    # match_phrase: semua token harus muncul berurutan
                    {
                        "match_phrase": {
                            "kabupaten.text": wilayah
                        }
                    },
                    {
                        "match_phrase": {
                            "kecamatan.text": wilayah
                        }
                    },
                    # fallback: keyword exact (normalized lowercase)
                    {
                        "term": {
                            "kabupaten": wilayah.lower()
                        }
                    }
                ],
                "minimum_should_match": 1
            }
        }
 
    # =====================================================
    # STRIP LOCATION PREFIX
    # =====================================================
    def _strip_location_prefix(self, text: str) -> str:
        text = re.sub(
            r"^(kota\s+adm\.?\s*|kota\s+|kab\.?\s+|kabupaten\s+|"
            r"prov\.?\s+|provinsi\s+|kec\.?\s+|kecamatan\s+)",
            "", text
        )
        return text.strip()
 
    # =====================================================
    # FILTER BUILDER
    # =====================================================
    def _build_filters(self, bentuk, year):
 
        filters = []
 
        if bentuk:
            filters.append({
                "term": {"bentuk_pendidikan": bentuk}
            })
 
        if year:
            filters.append({
                "term": {"tahun": year}
            })
 
        return filters
 
    # =====================================================
    # NORMALIZE TEXT
    # =====================================================
    def _normalize_text(self, text: str) -> str:
 
        if not text:
            return ""
 
        text = text.lower().strip()
        text = re.sub(r"\s+", " ", text)
 
        return text
    
    _INTENT_WORDS_RE = re.compile(
        r"\b(nomor\s+telepon|no\.?\s*telp?|website|situs\s+web|"
        r"alamat|email|fax|akreditasi|profil|profile|info(?:rmasi)?|"
        r"data|detail|apa(?:kah)?|di\s*mana|dimana|berapa|"
        r"dan|atau|dari|yang|untuk|tentang|mengenai|tampilkan|"
        r"cari|carikan|sebutkan|jelaskan)\b",
        re.IGNORECASE,
    )

    def _strip_intent_words(self, name: str) -> str:
        cleaned = self._INTENT_WORDS_RE.sub(" ", name)
        return re.sub(r"\s{2,}", " ", cleaned).strip()
    # =====================================================
    # EXECUTE SEARCH
    # =====================================================
    async def _execute(self, query: dict) -> dict:
 
        response = await self.es.search(
            index=self.index,
            body=query
        )
 
        hits = []
 
        for hit in response["hits"]["hits"]:
            hits.append(hit)

        total_val = response["hits"]["total"]
        logger.info("[PROFILE] track_total raw=%s", total_val)
        total_hits = total_val["value"] if isinstance(total_val, dict) else total_val
        logger.info("[PROFILE] total_found=%d hits_returned=%d", total_hits, len(hits))

        return {
            "total": len(hits),           # jumlah hits yang dikembalikan (max 20)
            "total_found": total_hits,    # total sebenarnya di ES
            "hits": hits
        }
