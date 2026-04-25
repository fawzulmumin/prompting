// mapping elasticsearch index : idx_qc_sekolah
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


// sampe data index : idx_sekolah_qc

{
        "_index": "idx_sekolah_qc",
        "_id": "70001798",
        "_score": 1,
        "_source": {
          "sekolah_id": "3c3f93bd-558a-42fe-9724-9e0d82c62a85",
          "npsn": "70001798",
          "nama": "TK IT TANWIRUL AFKAR",
          "bentuk_pendidikan": "TK",
          "status_sekolah": "SWASTA",
          "keaktifan": "1",
          "alamat_jalan": "JL. DUSUN DERMOSARI RT 19 RW 05 SUMBERAGUNG NGANTANG",
          "desa_kelurahan": "Sumberagung",
          "kecamatan": "Kec. Ngantang",
          "kabupaten": "Kab. Malang",
          "provinsi": "Prov. Jawa Timur",
          "kode_wilayah": "051832AI",
          "kode_pos": "65392",
          "wilayah_key": "prov. jawa timur|kab. malang|kec. ngantang|sumberagung",
          "lintang": 0.0075,
          "bujur": 0.0061,
          "lokasi": {
            "lat": 0.0075,
            "lon": 0.0061
          },
          "nomor_telepon": "021-0000000",
          "email": "sekolah@example.com",
          "website": "http://",
          "status_kepemilikan": "Yayasan",
          "yayasan": "PERKUMPULAN PENDIDIKAN DAN SOSIAL AL-URWATUL WUTSQO SUMBERAGUNG",
          "akreditasi": "B",
          "sk_pendirian_sekolah": "420/94/PAUD/3.07.101/2010",
          "tanggal_sk_pendirian": "2010-12-10",
          "sk_izin_operasional": "421.1/ 384 /35.07.101/2023",
          "tanggal_sk_izin_operasional": "2023-09-19",
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
            "qc_12": 2,
            "qc_13": 1,
            "qc_14": 0,
            "qc_15": 0
          },
          "qc_score": {
            "total_masalah": 1,
            "persen_bersih": 92.31
          },
          "content": "TK IT TANWIRUL AFKAR. TK. sekolah swasta. akreditasi B. status 1. di Sumberagung, Kec. Ngantang, Kab. Malang, Prov. Jawa Timur. alamat JL. DUSUN DERMOSARI RT 19 RW 05 SUMBERAGUNG NGANTANG. yayasan PERKUMPULAN PENDIDIKAN DAN SOSIAL AL-URWATUL WUTSQO SUMBERAGUNG. Masalah data: foto sekolah belum diunggah."
        }
      },
      {
        "_index": "idx_sekolah_qc",
        "_id": "69977587",
        "_score": 1,
        "_source": {
          "sekolah_id": "155411e7-187c-4219-b34b-4d32e8e5252e",
          "npsn": "69977587",
          "nama": "RA BAITUL HUDA",
          "bentuk_pendidikan": "RA",
          "status_sekolah": "SWASTA",
          "keaktifan": "1",
          "alamat_jalan": "JL. RAYA KROBYO`AN 448",
          "desa_kelurahan": "Jedong",
          "kecamatan": "Kec. Wagir",
          "kabupaten": "Kab. Malang",
          "provinsi": "Prov. Jawa Timur",
          "kode_wilayah": "051821AJ",
          "wilayah_key": "prov. jawa timur|kab. malang|kec. wagir|jedong",
          "status_kepemilikan": "Yayasan",
          "akreditasi": "B",
          "sk_pendirian_sekolah": "5484 TAHUN 2017",
          "tanggal_sk_pendirian": "2017-08-15",
          "sk_izin_operasional": "5484 TAHUN 2017",
          "tanggal_sk_izin_operasional": "2017-08-15",
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
            "qc_12": 2,
            "qc_13": 3,
            "qc_14": 0,
            "qc_15": 0
          },
          "qc_score": {
            "total_masalah": 4,
            "persen_bersih": 69.23
          },
          "content": "RA BAITUL HUDA. RA. sekolah swasta. akreditasi B. status 1. di Jedong, Kec. Wagir, Kab. Malang, Prov. Jawa Timur. alamat JL. RAYA KROBYO`AN 448. Masalah data: koordinat lintang kosong, koordinat bujur kosong, foto sekolah belum diunggah, yayasan tidak terdaftar."
        }
      },
      {
        "_index": "idx_sekolah_qc",
        "_id": "20569673",
        "_score": 1,
        "_source": {
          "sekolah_id": "f3de967f-7448-4bb5-80a4-920fe69aa910",
          "npsn": "20569673",
          "nama": "TK AISYIYAH BUSTANUL ATHFAL 24 PUJON",
          "bentuk_pendidikan": "TK",
          "status_sekolah": "SWASTA",
          "keaktifan": "1",
          "alamat_jalan": "DUSUN KRAJAN RT. 10 RW. 05, DESA PUJONKIDUL, KEC PUJON, KAB MALANG",
          "desa_kelurahan": "Pujon Kidul",
          "kecamatan": "Kec. Pujon",
          "kabupaten": "Kab. Malang",
          "provinsi": "Prov. Jawa Timur",
          "kode_wilayah": "051831AC",
          "kode_pos": "65391",
          "wilayah_key": "prov. jawa timur|kab. malang|kec. pujon|pujon kidul",
          "lintang": -7.8589,
          "bujur": 112.4646,
          "lokasi": {
            "lat": -7.8589,
            "lon": 112.4646
          },
          "nomor_telepon": "021-0000000",
          "email": "sekolah@example.com",
          "website": "http://",
          "status_kepemilikan": "Yayasan",
          "yayasan": "Pimpinan Daerah Muhammadiyah (PDM) Kab. Malang",
          "akreditasi": "B",
          "sk_pendirian_sekolah": "500.16.7.2/10/IPTK/35.07.317/2024",
          "tanggal_sk_pendirian": "2024-10-24",
          "sk_izin_operasional": "400.3.2/2614/35.07.301/2024",
          "tanggal_sk_izin_operasional": "2024-05-02",
          "waktu_penyelenggaraan": "Pagi/5 hari",
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
          "content": "TK AISYIYAH BUSTANUL ATHFAL 24 PUJON. TK. sekolah swasta. akreditasi B. status 1. di Pujon Kidul, Kec. Pujon, Kab. Malang, Prov. Jawa Timur. alamat DUSUN KRAJAN RT. 10 RW. 05, DESA PUJONKIDUL, KEC PUJON, KAB MALANG. yayasan Pimpinan Daerah Muhammadiyah (PDM) Kab. Malang. Data lengkap."
        }
      },

//belum di vectorisasi karena menunggu dari rekan untuk vectorisasi 