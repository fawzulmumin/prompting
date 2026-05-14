"""
Test PromptLoader
Jalankan: pytest tests/test_prompt_loader.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.utils.prompt_loader import PromptLoader, load_prompt


class TestPromptLoader:
    def setup_method(self):
        self.loader = PromptLoader()

    def test_load_intent_classifier_active(self):
        """Load versi aktif intent_classifier harus berhasil."""
        prompt = self.loader.load("intent_classifier")
        assert isinstance(prompt, str)
        assert len(prompt) > 100, "Prompt harus punya konten yang cukup"

    def test_load_intent_classifier_v1_explicit(self):
        """Load versi v1 secara eksplisit harus sama dengan aktif."""
        active = self.loader.load("intent_classifier")
        v1 = self.loader.load("intent_classifier", version="v1")
        assert active == v1

    def test_get_active_version(self):
        version = self.loader.get_active_version("intent_classifier")
        assert version == "v1"

    def test_list_versions(self):
        versions = self.loader.list_versions("intent_classifier")
        assert "v1" in versions

    def test_load_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            self.loader.load("nonexistent_prompt_xyz")

    def test_load_wrong_version_raises(self):
        with pytest.raises((ValueError, FileNotFoundError)):
            self.loader.load("intent_classifier", version="v999")

    def test_convenience_function(self):
        prompt = load_prompt("intent_classifier")
        assert len(prompt) > 100

    def test_prompt_content_sanity(self):
        """Prompt harus mengandung keyword kunci yang diharapkan."""
        prompt = load_prompt("intent_classifier")
        # Cek beberapa keyword yang harus ada di prompt classifier
        prompt_lower = prompt.lower()
        assert any(kw in prompt_lower for kw in ["aggregation", "list", "profile", "intent"]), \
            "Prompt harus mengandung keyword intent classification"
