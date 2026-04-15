import json

def build_profile_prompt(profile_data: dict) -> str:
    """
    Build structured prompt for LLM narrative generation.
    """

    formatted_data = json.dumps(profile_data, indent=2, ensure_ascii=False)

    return f"""
Anda adalah analis pendidikan profesional.

Buatkan narasi deskriptif yang formal dan informatif
berdasarkan data profil sekolah berikut.

Data:
{formatted_data}

Tuliskan dalam 1–3 paragraf yang jelas dan ringkas.
"""
