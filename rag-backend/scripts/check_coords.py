import pandas as pd
import re

# Sesuaikan dengan path file kamu
csv_path = r'C:\users\fam\downloads\elasticsearch.kb\es-export\derived\datamart_sekolah_with_derived.csv'

def is_valid_geo_point(value):
    if pd.isna(value) or value == "":
        return False, "Kosong/NaN"
    
    # Format geo_point biasanya "lat,lon"
    # Contoh valid: "-6.123,106.123"
    try:
        parts = str(value).split(',')
        if len(parts) != 2:
            return False, f"Format salah (butuh 1 koma, ada {len(parts)-1})"
        
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
        
        # Validasi rentang koordinat bumi
        if not (-90 <= lat <= 90):
            return False, f"Latitude di luar jangkauan: {lat}"
        if not (-180 <= lon <= 180):
            return False, f"Longitude di luar jangkauan: {lon}"
            
        return True, "Valid"
    except ValueError:
        return False, "Bukan angka"

print(f"--- Memulai Validasi Kolom 'lokasi' ---")
# Gunakan chunksize agar RAM hemat (tidak load 2.8jt baris sekaligus)
chunk_size = 100000
error_found = 0

try:
    reader = pd.read_csv(csv_path, chunksize=chunk_size, low_memory=False)
    for i, chunk in enumerate(reader):
        if 'lokasi' not in chunk.columns:
            print("Eror: Kolom 'lokasi' tidak ditemukan!")
            break
            
        # Periksa baris yang tidak valid
        mask = chunk['lokasi'].apply(lambda x: is_valid_geo_point(x)[0])
        invalid_rows = chunk[~mask]
        
        if not invalid_rows.empty:
            for idx, row in invalid_rows.iterrows():
                error_found += 1
                status, pesan = is_valid_geo_point(row['lokasi'])
                print(f"Baris {idx} | Data: '{row['lokasi']}' | Masalah: {pesan}")
                
                # Stop cetak jika terlalu banyak eror agar layar tidak penuh
                if error_found >= 20:
                    print("... dan seterusnya. Terlalu banyak eror ditemukan.")
                    break
        
        if error_found >= 20: break
        print(f"Sudah memeriksa { (i+1) * chunk_size } baris...")

    if error_found == 0:
        print(" Semua kolom 'lokasi' tampak valid!")
    else:
        print(f" Total ditemukan baris bermasalah.")

except Exception as e:
    print(f"Terjadi kesalahan saat membaca file: {e}")