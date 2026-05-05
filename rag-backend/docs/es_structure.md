// mapping elasticsearch index : idx_sekolah_profile
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


// sampe data index : idx_sekolah_profile

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
            ...dst
          ]
        }
}