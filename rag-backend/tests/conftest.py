# conftest.py
# Simpan di root project (elasticsearch.kb/conftest.py)
# Mendaftarkan custom marks agar tidak ada PytestUnknownMarkWarning

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from dotenv import load_dotenv

load_dotenv(".env.production")

def pytest_configure(config):
    config.addinivalue_line("markers", "unit: unit test — tidak butuh LLM atau ES")
    config.addinivalue_line("markers", "integration: integration test — butuh API key / koneksi")
