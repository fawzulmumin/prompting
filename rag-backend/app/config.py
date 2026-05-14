# config.py
import logging
import os
from dotenv import load_dotenv
from pathlib import Path

_log = logging.getLogger(__name__)

# ROOT project adalah satu level di atas folder app/
ROOT_DIR = Path(__file__).resolve().parent.parent

PROD = os.getenv("PROD") or os.getenv("PROD_ENV") or "development"

env_file_name = ".env.production" if PROD == "production" else ".env"
env_path = ROOT_DIR / env_file_name

if env_path.exists():
    _log.info("Config loaded from %s", env_path)
    load_dotenv(dotenv_path=env_path, override=True)
else:
    _log.warning("Env file not found: %s", env_path)

ELASTICSEARCH_NODE = os.getenv("ELASTICSEARCH_NODE")
ELASTICSEARCH_USERNAME = os.getenv("ELASTICSEARCH_USERNAME")
ELASTICSEARCH_PASSWORD = os.getenv("ELASTICSEARCH_PASSWORD")
ELASTICSEARCH_INDEX = os.getenv("ELASTICSEARCH_INDEX")
ELASTICSEARCH_AGENT_INDEX = os.getenv("ELASTICSEARCH_AGENT_INDEX", "idx_prompting_agent")
ELASTICSEARCH_PROFILE_INDEX = os.getenv("ELASTICSEARCH_PROFILE_INDEX", "idx_sekolah_profile")

ENV = PROD