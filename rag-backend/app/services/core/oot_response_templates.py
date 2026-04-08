# services/core/oot_response_templates.py

_OOT_TEMPLATES = {
    "default": (
        "Mohon maaf, pertanyaan Anda berada di luar cakupan layanan ini. "
        "Sistem Informasi Satuan Pendidikan Kemendikdasmen hanya melayani pertanyaan "
        "yang berkaitan dengan data sekolah, tenaga pendidik, dan peserta didik "
        "di seluruh wilayah Indonesia.\n\n"
        "Anda dapat mencoba pertanyaan seperti:\n"
        "- *Berapa SMA Negeri di Kabupaten Bandung?*\n"
        "- *Daftar SMK akreditasi A di Jakarta Selatan*\n"
        "- *Profil SMAN 1 Bogor*"
    ),
    "politik": (
        "Mohon maaf, pertanyaan mengenai politik atau pemerintahan berada di luar "
        "cakupan Sistem Informasi Satuan Pendidikan ini. Kami hanya melayani "
        "informasi terkait data pendidikan dasar dan menengah Indonesia."
    ),
    "ekonomi_keuangan": (
        "Mohon maaf, pertanyaan mengenai kondisi ekonomi atau keuangan berada di luar "
        "cakupan layanan ini. Sistem kami berfokus pada data satuan pendidikan "
        "dalam naungan Kemendikdasmen."
    ),
    "kesehatan": (
        "Mohon maaf, pertanyaan medis atau kesehatan berada di luar cakupan layanan "
        "Sistem Informasi Satuan Pendidikan. Untuk informasi kesehatan, silakan "
        "mengunjungi layanan Kemenkes atau fasilitas kesehatan terdekat."
    ),
    "hiburan": (
        "Mohon maaf, pertanyaan mengenai hiburan berada di luar cakupan sistem ini. "
        "Kami melayani informasi seputar satuan pendidikan (sekolah, guru, siswa) "
        "di Indonesia."
    ),
}

def get_oot_response(oot_reason: str) -> str:
    return _OOT_TEMPLATES.get(oot_reason, _OOT_TEMPLATES["default"])
