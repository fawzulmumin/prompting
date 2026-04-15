# conftest.py
# Simpan di root project (elasticsearch.kb/conftest.py)
# Mendaftarkan custom marks agar tidak ada PytestUnknownMarkWarning

import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "unit: unit test — tidak butuh LLM atau ES")
    config.addinivalue_line("markers", "integration: integration test — butuh API key / koneksi")