"""
app/utils/prompt_loader.py
===========================
Loader untuk prompt files eksternal.
Mendukung versioning via manifest.yaml.

Penggunaan:
    loader = PromptLoader()
    prompt = loader.load("intent_classifier")   # load versi aktif
    prompt = loader.load("intent_classifier", version="v1")  # load versi spesifik
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Root folder prompts/ relatif ke rag-backend/
_PROMPTS_ROOT = Path(__file__).parent.parent.parent / "prompts"


class PromptLoader:
    """Load prompt text dari file eksternal dengan dukungan versioning."""

    def __init__(self, prompts_root: Optional[Path] = None):
        self.root = prompts_root or _PROMPTS_ROOT

    def load(self, name: str, version: Optional[str] = None) -> str:
        """
        Load prompt text.

        Args:
            name: nama subfolder di prompts/ (contoh: "intent_classifier")
            version: versi spesifik ("v1", "v2") atau None untuk aktif

        Returns:
            Isi prompt sebagai string

        Raises:
            FileNotFoundError: jika file tidak ditemukan
        """
        folder = self.root / name

        if not folder.exists():
            raise FileNotFoundError(f"Prompt folder tidak ditemukan: {folder}")

        # Resolve versi
        if version is None:
            version = self._get_active_version(folder)

        manifest = self._load_manifest(folder)
        version_info = manifest.get("versions", {}).get(version)
        if not version_info:
            raise ValueError(f"Versi '{version}' tidak ada di manifest {name}")

        prompt_file = folder / version_info["file"]
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file tidak ditemukan: {prompt_file}")

        content = prompt_file.read_text(encoding="utf-8")
        logger.debug(
            "[PROMPT_LOADER] loaded name=%s version=%s chars=%d",
            name, version, len(content),
        )
        return content

    def _get_active_version(self, folder: Path) -> str:
        manifest = self._load_manifest(folder)
        active = manifest.get("active")
        if not active:
            raise ValueError(f"Manifest di {folder} tidak punya field 'active'")
        return active

    @lru_cache(maxsize=32)
    def _load_manifest(self, folder: Path) -> dict:
        manifest_path = folder / "manifest.yaml"
        if not manifest_path.exists():
            raise FileNotFoundError(f"manifest.yaml tidak ditemukan di {folder}")
        with open(manifest_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def list_versions(self, name: str) -> list[str]:
        """List semua versi yang tersedia untuk prompt tertentu."""
        folder = self.root / name
        manifest = self._load_manifest(folder)
        return list(manifest.get("versions", {}).keys())

    def get_active_version(self, name: str) -> str:
        folder = self.root / name
        return self._get_active_version(folder)


# Singleton untuk dipakai di seluruh app
_default_loader = PromptLoader()


def load_prompt(name: str, version: Optional[str] = None) -> str:
    """Convenience function menggunakan default loader."""
    return _default_loader.load(name, version)
