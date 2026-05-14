// mapping index : idx_prompting_agent
{
  "idx_prompting_agent": {
    "mappings": {
      "dynamic": "strict",
      "properties": {
        "akreditasi": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "akses_internet": {
          "type": "keyword"
        },
        "alamat_jalan": {
          "type": "text",
          "index_options": "docs",
          "analyzer": "indonesian_text"
        },
        "bentuk_pendidikan": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "bujur": {
          "type": "float",
          "index": false
        },
        "content": {
          "type": "text",
          "analyzer": "indonesian_text"
        },
        "content_vector": {
          "type": "dense_vector",
          "dims": 384,
          "index": true,
          "similarity": "cosine",
          "index_options": {
            "type": "int8_hnsw",
            "m": 16,
            "ef_construction": 100
          }
        },
        "desa_kelurahan": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "guru_honor": {
          "type": "integer"
        },
        "guru_l": {
          "type": "integer"
        },
        "guru_p": {
          "type": "integer"
        },
        "guru_p3k": {
          "type": "integer"
        },
        "guru_pns": {
          "type": "integer"
        },
        "guru_s1_keatas": {
          "type": "integer"
        },
        "guru_sertifikasi": {
          "type": "integer"
        },
        "guru_total": {
          "type": "integer"
        },
        "kabupaten": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "kecamatan": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "kecukupan_air": {
          "type": "integer"
        },
        "kode_pos": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "last_update_sekolah": {
          "type": "date",
          "index": false,
          "doc_values": false
        },
        "lintang": {
          "type": "float",
          "index": false
        },
        "lokasi": {
          "type": "geo_point"
        },
        "minum_siswa": {
          "type": "integer"
        },
        "nama": {
          "type": "text",
          "fields": {
            "autocomplete": {
              "type": "text",
              "analyzer": "edge_ngram_analyzer",
              "search_analyzer": "indonesian_text"
            },
            "keyword": {
              "type": "keyword",
              "ignore_above": 256,
              "normalizer": "lowercase_normalizer"
            }
          },
          "analyzer": "indonesian_text"
        },
        "nilai_akhir": {
          "type": "float"
        },
        "npsn": {
          "type": "keyword"
        },
        "penyelenggara": {
          "type": "keyword",
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "provinsi": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "sekolah_id": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "semester_id": {
          "type": "keyword"
        },
        "siswa_baru_l": {
          "type": "integer"
        },
        "siswa_baru_p": {
          "type": "integer"
        },
        "siswa_l": {
          "type": "integer"
        },
        "siswa_lulus_l": {
          "type": "integer"
        },
        "siswa_lulus_p": {
          "type": "integer"
        },
        "siswa_mengulang_l": {
          "type": "integer"
        },
        "siswa_mengulang_p": {
          "type": "integer"
        },
        "siswa_p": {
          "type": "integer"
        },
        "siswa_total": {
          "type": "integer"
        },
        "sk_penetapan": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "status_sekolah": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "sumber_air": {
          "type": "keyword"
        },
        "sumber_listrik": {
          "type": "keyword"
        },
        "tahun": {
          "type": "integer"
        },
        "tendik_l": {
          "type": "integer"
        },
        "tendik_p": {
          "type": "integer"
        },
        "tipe_jamban": {
          "type": "integer"
        },
        "toilet_siswa_laki": {
          "type": "integer"
        },
        "toilet_siswa_perempuan": {
          "type": "integer"
        },
        "total_komputer": {
          "type": "integer"
        },
        "wilayah_key": {
          "type": "keyword"
        }
      }
    }
  }
}

// mapping index : idx_sekolah_profile
{
  "idx_sekolah_profile": {
    "mappings": {
      "dynamic": "strict",
      "properties": {
        "_meta_sync": {
          "properties": {
            "source": {
              "type": "keyword",
              "index": false,
              "doc_values": false
            },
            "synced_at": {
              "type": "date"
            }
          }
        },
        "akreditasi": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "akses_internet": {
          "type": "keyword"
        },
        "alamat_jalan": {
          "type": "text",
          "index_options": "docs",
          "analyzer": "indonesian_text"
        },
        "bentuk_pendidikan": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "bujur": {
          "type": "float",
          "index": false
        },
        "content": {
          "type": "text",
          "analyzer": "indonesian_text"
        },
        "content_vector": {
          "type": "dense_vector",
          "dims": 384,
          "index": true,
          "similarity": "cosine",
          "index_options": {
            "type": "int8_hnsw",
            "m": 16,
            "ef_construction": 100
          }
        },
        "desa_kelurahan": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "email": {
          "type": "keyword"
        },
        "guru_honor": {
          "type": "integer"
        },
        "guru_l": {
          "type": "integer"
        },
        "guru_p": {
          "type": "integer"
        },
        "guru_p3k": {
          "type": "integer"
        },
        "guru_pns": {
          "type": "integer"
        },
        "guru_s1_keatas": {
          "type": "integer"
        },
        "guru_sertifikasi": {
          "type": "integer"
        },
        "guru_total": {
          "type": "integer"
        },
        "kabupaten": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "keaktifan": {
          "type": "keyword"
        },
        "kecamatan": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "kecukupan_air": {
          "type": "integer"
        },
        "kode_pos": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "kode_wilayah": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "last_update_sekolah": {
          "type": "date",
          "index": false,
          "doc_values": false
        },
        "lintang": {
          "type": "float",
          "index": false
        },
        "lokasi": {
          "type": "geo_point"
        },
        "minum_siswa": {
          "type": "integer"
        },
        "nama": {
          "type": "text",
          "fields": {
            "autocomplete": {
              "type": "text",
              "analyzer": "edge_ngram_analyzer",
              "search_analyzer": "indonesian_text"
            },
            "keyword": {
              "type": "keyword",
              "ignore_above": 256,
              "normalizer": "lowercase_normalizer"
            }
          },
          "analyzer": "indonesian_text"
        },
        "nilai_akhir": {
          "type": "float"
        },
        "nomor_telepon": {
          "type": "keyword"
        },
        "npsn": {
          "type": "keyword"
        },
        "partisipasi_bos": {
          "type": "boolean"
        },
        "penyelenggara": {
          "type": "keyword",
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "provinsi": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "qc": {
          "properties": {
            "qc_1": {
              "type": "byte"
            },
            "qc_10": {
              "type": "byte"
            },
            "qc_11": {
              "type": "byte"
            },
            "qc_12": {
              "type": "byte"
            },
            "qc_13": {
              "type": "byte"
            },
            "qc_14": {
              "type": "byte"
            },
            "qc_15": {
              "type": "byte"
            },
            "qc_2": {
              "type": "byte"
            },
            "qc_3": {
              "type": "byte"
            },
            "qc_4": {
              "type": "byte"
            },
            "qc_5": {
              "type": "byte"
            },
            "qc_6": {
              "type": "byte"
            },
            "qc_7": {
              "type": "byte"
            },
            "qc_8": {
              "type": "byte"
            },
            "qc_9": {
              "type": "byte"
            }
          }
        },
        "qc_score": {
          "properties": {
            "persen_bersih": {
              "type": "float"
            },
            "total_masalah": {
              "type": "integer"
            }
          }
        },
        "sekolah_id": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "semester_id": {
          "type": "keyword"
        },
        "sertifikasi_iso": {
          "type": "keyword"
        },
        "siswa_baru_l": {
          "type": "integer"
        },
        "siswa_baru_p": {
          "type": "integer"
        },
        "siswa_l": {
          "type": "integer"
        },
        "siswa_lulus_l": {
          "type": "integer"
        },
        "siswa_lulus_p": {
          "type": "integer"
        },
        "siswa_mengulang_l": {
          "type": "integer"
        },
        "siswa_mengulang_p": {
          "type": "integer"
        },
        "siswa_p": {
          "type": "integer"
        },
        "siswa_total": {
          "type": "integer"
        },
        "sk_izin_operasional": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "sk_pendirian_sekolah": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "sk_penetapan": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "status_kepemilikan": {
          "type": "keyword"
        },
        "status_sekolah": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "sumber_air": {
          "type": "keyword"
        },
        "sumber_listrik": {
          "type": "keyword"
        },
        "tahun": {
          "type": "integer"
        },
        "tanggal_sk_izin_operasional": {
          "type": "date",
          "format": "yyyy-MM-dd||yyyy-MM-dd HH:mm:ss||yyyy-MM-dd HH.mm.ss.SSS||yyyy-MM-dd'T'HH:mm:ss.SSS'Z'||epoch_millis"
        },
        "tanggal_sk_pendirian": {
          "type": "date",
          "format": "yyyy-MM-dd||yyyy-MM-dd HH:mm:ss||yyyy-MM-dd HH.mm.ss.SSS||yyyy-MM-dd'T'HH:mm:ss.SSS'Z'||epoch_millis"
        },
        "tendik_l": {
          "type": "integer"
        },
        "tendik_p": {
          "type": "integer"
        },
        "tipe_jamban": {
          "type": "integer"
        },
        "toilet_siswa_laki": {
          "type": "integer"
        },
        "toilet_siswa_perempuan": {
          "type": "integer"
        },
        "total_komputer": {
          "type": "integer"
        },
        "waktu_penyelenggaraan": {
          "type": "keyword"
        },
        "website": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "wilayah_key": {
          "type": "keyword"
        },
        "wilayah_perbatasan": {
          "type": "boolean"
        },
        "wilayah_terpencil": {
          "type": "boolean"
        },
        "yayasan": {
          "type": "text",
          "fields": {
            "keyword": {
              "type": "keyword",
              "ignore_above": 256,
              "normalizer": "lowercase_normalizer"
            }
          },
          "analyzer": "indonesian_text"
        }
      }
    }
  }
}

// mapping index : idx_sekolah_qc
{
  "idx_sekolah_qc": {
    "mappings": {
      "dynamic": "strict",
      "properties": {
        "akreditasi": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "akses_internet": {
          "type": "keyword"
        },
        "alamat_jalan": {
          "type": "text",
          "index_options": "docs",
          "analyzer": "indonesian_text"
        },
        "bentuk_pendidikan": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "bujur": {
          "type": "float",
          "index": false
        },
        "content": {
          "type": "text",
          "analyzer": "indonesian_text"
        },
        "content_vector": {
          "type": "dense_vector",
          "dims": 384,
          "index": true,
          "similarity": "cosine",
          "index_options": {
            "type": "int8_hnsw",
            "m": 16,
            "ef_construction": 100
          }
        },
        "desa_kelurahan": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "email": {
          "type": "keyword"
        },
        "kabupaten": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "keaktifan": {
          "type": "keyword"
        },
        "kecamatan": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "kode_pos": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "kode_wilayah": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "lintang": {
          "type": "float",
          "index": false
        },
        "lokasi": {
          "type": "geo_point"
        },
        "nama": {
          "type": "text",
          "fields": {
            "autocomplete": {
              "type": "text",
              "analyzer": "edge_ngram_analyzer",
              "search_analyzer": "indonesian_text"
            },
            "keyword": {
              "type": "keyword",
              "ignore_above": 256,
              "normalizer": "lowercase_normalizer"
            }
          },
          "analyzer": "indonesian_text"
        },
        "nomor_telepon": {
          "type": "keyword"
        },
        "npsn": {
          "type": "keyword"
        },
        "partisipasi_bos": {
          "type": "boolean"
        },
        "provinsi": {
          "type": "keyword",
          "eager_global_ordinals": true,
          "normalizer": "lowercase_normalizer",
          "fields": {
            "text": {
              "type": "text",
              "analyzer": "indonesian_text"
            }
          }
        },
        "qc": {
          "properties": {
            "qc_1": {
              "type": "byte"
            },
            "qc_10": {
              "type": "byte"
            },
            "qc_11": {
              "type": "byte"
            },
            "qc_12": {
              "type": "byte"
            },
            "qc_13": {
              "type": "byte"
            },
            "qc_14": {
              "type": "byte"
            },
            "qc_15": {
              "type": "byte"
            },
            "qc_2": {
              "type": "byte"
            },
            "qc_3": {
              "type": "byte"
            },
            "qc_4": {
              "type": "byte"
            },
            "qc_5": {
              "type": "byte"
            },
            "qc_6": {
              "type": "byte"
            },
            "qc_7": {
              "type": "byte"
            },
            "qc_8": {
              "type": "byte"
            },
            "qc_9": {
              "type": "byte"
            }
          }
        },
        "qc_score": {
          "properties": {
            "persen_bersih": {
              "type": "float"
            },
            "total_masalah": {
              "type": "integer"
            }
          }
        },
        "sekolah_id": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "sertifikasi_iso": {
          "type": "keyword"
        },
        "sk_izin_operasional": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "sk_pendirian_sekolah": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "status_kepemilikan": {
          "type": "keyword"
        },
        "status_sekolah": {
          "type": "keyword",
          "eager_global_ordinals": true
        },
        "sumber_listrik": {
          "type": "keyword"
        },
        "tanggal_sk_izin_operasional": {
          "type": "date",
          "format": "yyyy-MM-dd||yyyy-MM-dd HH:mm:ss||yyyy-MM-dd HH.mm.ss.SSS||yyyy-MM-dd'T'HH:mm:ss.SSS'Z'||epoch_millis"
        },
        "tanggal_sk_pendirian": {
          "type": "date",
          "format": "yyyy-MM-dd||yyyy-MM-dd HH:mm:ss||yyyy-MM-dd HH.mm.ss.SSS||yyyy-MM-dd'T'HH:mm:ss.SSS'Z'||epoch_millis"
        },
        "waktu_penyelenggaraan": {
          "type": "keyword"
        },
        "website": {
          "type": "keyword",
          "index": false,
          "doc_values": false
        },
        "wilayah_key": {
          "type": "keyword"
        },
        "wilayah_perbatasan": {
          "type": "boolean"
        },
        "wilayah_terpencil": {
          "type": "boolean"
        },
        "yayasan": {
          "type": "text",
          "fields": {
            "keyword": {
              "type": "keyword",
              "ignore_above": 256,
              "normalizer": "lowercase_normalizer"
            }
          },
          "analyzer": "indonesian_text"
        }
      }
    }
  }
}

// sample data index : idx_prompting_agent
"hits": [
      {
        "_index": "idx_prompting_agent",
        "_id": "50102785",
        "_score": 1,
        "_routing": "prov._bali",
        "_source": {
          "tipe_jamban": 1,
          "status_sekolah": "NEGERI",
          "sekolah_id": "20AE0115-31F5-E011-A9E5-C75B7494ECEA",
          "nama": "SD NEGERI 7 BAN",
          "kabupaten": "Kab. Karangasem",
          "akreditasi": "C",
          "toilet_siswa_laki": 0,
          "last_update_sekolah": "2021-11-08T01:58:50.057Z",
          "sumber_air": "Sumur terlindungi",
          "kecukupan_air": 2,
          "npsn": "50102785",
          "desa_kelurahan": "Ban",
          "tahun": 2020,
          "bentuk_pendidikan": "SD",
          "semester_id": "20252",
          "provinsi": "Prov. Bali",
          "toilet_siswa_perempuan": 0,
          "kode_pos": "80853",
          "nilai_akhir": 73,
          "sk_penetapan": "595/BAN-PDM/SK/2025",
          "kecamatan": "Kec. Kubu",
          "minum_siswa": 0,
          "alamat_jalan": "Banjar Dinas Temakung",
          "lintang": -8.2711,
          "bujur": 115.4827,
          "lokasi": {
            "lat": -8.2711,
            "lon": 115.4827
          },
          "wilayah_key": "prov. bali|kab. karangasem|kec. kubu|ban",
          "siswa_l": 76,
          "siswa_p": 67,
          "siswa_total": 143,
          "siswa_baru_l": 16,
          "siswa_baru_p": 8,
          "siswa_lulus_l": 0,
          "siswa_lulus_p": 0,
          "siswa_mengulang_l": 0,
          "siswa_mengulang_p": 2,
          "guru_l": 8,
          "guru_p": 0,
          "guru_total": 8,
          "guru_pns": 6,
          "guru_p3k": 2,
          "guru_honor": 0,
          "guru_sertifikasi": 7,
          "guru_s1_keatas": 8,
          "tendik_l": 2,
          "tendik_p": 0,
          "content": """{"akreditasi":"C","alamat_jalan":"Banjar Dinas Temakung","bentuk_pendidikan":"SD","desa_kelurahan":"Ban","kabupaten":"Kab. Karangasem","kecamatan":"Kec. Kubu","nama":"SD NEGERI 7 BAN","provinsi":"Prov. Bali","status_sekolah":"NEGERI","sumber_air":"Sumur terlindungi","jumlah_siswa":143,"jumlah_guru":8}""",
          "content_vector": [
            -0.004491256549954414,
            0.11866862326860428,
            -0.037320397794246674,
            dan seterusnya angka vector
          ]
        }
      }
]

// sample data index : idx_sekolah_profile
"hits": [
      {
        "_index": "idx_sekolah_profile",
        "_id": "70007837",
        "_score": 1,
        "_source": {
          "sekolah_id": "353d9a7b-fb53-4416-8274-eaecb0900d82",
          "npsn": "70007837",
          "nama": "TK MIFTAHUL ULUM",
          "bentuk_pendidikan": "TK",
          "status_sekolah": "SWASTA",
          "keaktifan": "1",
          "alamat_jalan": "Jl. Komplek Kranggan Barat",
          "desa_kelurahan": "Kranggan Barat",
          "kecamatan": "Kec. Tanah Merah",
          "kabupaten": "Kab. Bangkalan",
          "provinsi": "Prov. Jawa Timur",
          "kode_wilayah": "052908AD",
          "wilayah_key": "prov. jawa timur|kab. bangkalan|kec. tanah merah|kranggan barat",
          "lintang": -7.1035,
          "bujur": 112.8947,
          "lokasi": {
            "lat": -7.1035,
            "lon": 112.8947
          },
          "website": "http://",
          "status_kepemilikan": "Yayasan",
          "yayasan": "MIFTAHUL ULUM AL-FAUZAN",
          "akreditasi": "C",
          "sk_pendirian_sekolah": "421.1/2740/433.101/2018",
          "tanggal_sk_pendirian": "2018-07-31",
          "sk_izin_operasional": "421.1/7007/433.101/2024",
          "tanggal_sk_izin_operasional": "2024-11-06",
          "waktu_penyelenggaraan": "Pagi/6 hari",
          "sertifikasi_iso": "Belum Bersertifikat",
          "wilayah_terpencil": false,
          "wilayah_perbatasan": false,
          "partisipasi_bos": true,
          "qc": {
            "qc_1": 1,
            "qc_2": 1,
            "qc_3": 1,
            "qc_4": 1,
            "qc_5": 1,
            "qc_6": 1,
            "qc_7": 1,
            "qc_8": 1,
            "qc_9": 1,
            "qc_10": 1,
            "qc_11": 1,
            "qc_12": 1,
            "qc_13": 1,
            "qc_14": 0,
            "qc_15": 0
          },
          "qc_score": {
            "total_masalah": 0,
            "persen_bersih": 100
          },
          "content": "TK MIFTAHUL ULUM. TK. sekolah swasta. akreditasi C. status 1. di Kranggan Barat, Kec. Tanah Merah, Kab. Bangkalan, Prov. Jawa Timur. alamat Jl. Komplek Kranggan Barat. yayasan MIFTAHUL ULUM AL-FAUZAN. Data lengkap.",
          "content_vector": [
            -0.020225120708346367,
            0.03437444567680359,
            -0.03251834586262703,
            dan seterusnya angka vector
          ],
          "siswa_total": 35,
          "siswa_l": 22,
          "siswa_p": 13,
          "siswa_baru_l": 22,
          "siswa_baru_p": 13,
          "siswa_lulus_l": 1,
          "siswa_lulus_p": 2,
          "siswa_mengulang_l": 0,
          "siswa_mengulang_p": 0,
          "guru_total": 1,
          "guru_l": 0,
          "guru_p": 1,
          "guru_pns": 0,
          "guru_p3k": 0,
          "guru_honor": 0,
          "guru_sertifikasi": 0,
          "guru_s1_keatas": 0,
          "tendik_l": 0,
          "tendik_p": 1,
          "tipe_jamban": 1,
          "kecukupan_air": 2,
          "minum_siswa": 0,
          "sumber_air": "Sumur terlindungi",
          "toilet_siswa_laki": 0,
          "toilet_siswa_perempuan": 0,
          "semester_id": "20252",
          "tahun": 2025,
          "nilai_akhir": 664,
          "sk_penetapan": "561/BAN-PDM/SK/2025",
          "last_update_sekolah": "2025-11-13T11:14:26.050Z",
          "_meta_sync": {
            "synced_at": "2026-05-04T15:32:18.978215Z",
            "source": "idx_sekolah_qc+idx_prompting_agent"
          }
        }
      },
]

// sample data index : idx_sekolah_qc
"hits": [
      {
        "_index": "idx_sekolah_qc",
        "_id": "70038948",
        "_score": 1,
        "_source": {
          "sekolah_id": "fa9701bf-09e6-4a74-8832-d8fa1d8e668d",
          "npsn": "70038948",
          "nama": "KB INSAN MADANI",
          "bentuk_pendidikan": "KB",
          "status_sekolah": "SWASTA",
          "keaktifan": "1",
          "alamat_jalan": "Kp. Parengpeng Rt.001/001",
          "desa_kelurahan": "Pangkal Jaya",
          "kecamatan": "Kec. Nanggung",
          "kabupaten": "Kab. Bogor",
          "provinsi": "Prov. Jawa Barat",
          "kode_wilayah": "020501AF",
          "wilayah_key": "prov. jawa barat|kab. bogor|kec. nanggung|pangkal jaya",
          "website": "http://",
          "status_kepemilikan": "Yayasan",
          "yayasan": "YAYASAN CENDEKIA BOGOR",
          "akreditasi": "B",
          "sk_pendirian_sekolah": "076/SKEP/yaasin/VII/2021",
          "tanggal_sk_pendirian": "2021-07-02",
          "sk_izin_operasional": "421.1/35/Kpts/PAUD-NF/Kec.Nanggung/2021",
          "tanggal_sk_izin_operasional": "2021-12-28",
          "waktu_penyelenggaraan": "Sehari Penuh/5 hari",
          "sertifikasi_iso": "Belum Bersertifikat",
          "wilayah_terpencil": false,
          "wilayah_perbatasan": false,
          "partisipasi_bos": true,
          "qc": {
            "qc_1": 1,
            "qc_2": 1,
            "qc_3": 1,
            "qc_4": 1,
            "qc_5": 1,
            "qc_6": 2,
            "qc_7": 2,
            "qc_8": 1,
            "qc_9": 1,
            "qc_10": 1,
            "qc_11": 1,
            "qc_12": 1,
            "qc_13": 1,
            "qc_14": 0,
            "qc_15": 0
          },
          "qc_score": {
            "total_masalah": 2,
            "persen_bersih": 84.62
          },
          "content": "KB INSAN MADANI. KB. sekolah swasta. akreditasi B. status 1. di Pangkal Jaya, Kec. Nanggung, Kab. Bogor, Prov. Jawa Barat. alamat Kp. Parengpeng Rt.001/001. yayasan YAYASAN CENDEKIA BOGOR. Masalah data: koordinat lintang kosong, koordinat bujur kosong.",
          "content_vector": [
            -0.03452510014176369,
            0.04887421429157257,
            -0.033089280128479004,
            dan seterusnya vector angkat
          ]
        }
      },
]

