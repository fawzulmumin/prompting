#!/usr/bin/env python3
import os
import json
import argparse
import sys
from pathlib import Path
# 1. TAMBAHKAN ROOT PATH AGAR BISA IMPORT MODULE DARI LEVEL ATAS
root_path = Path(__file__).parent.parent
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))

root_path = Path(__file__).parent.parent
if str(root_path) not in sys.path:
    sys.path.append(str(root_path))
    
import app.config as config  
from elasticsearch import Elasticsearch, exceptions # pyright: ignore[reportMissingImports]

def init_index(force: bool = False) -> None:
    print(f"DEBUG: Mode Env = {os.getenv('PROD')}")
    print(f"DEBUG: Index Name Target = {os.getenv('ELASTICSEARCH_INDEX')}")
    es = Elasticsearch(
        hosts=[os.getenv("ELASTICSEARCH_NODE")],
        basic_auth=(
            os.getenv("ELASTICSEARCH_USERNAME"),    
            os.getenv("ELASTICSEARCH_PASSWORD")
        ),
        request_timeout=30
    )
    
    index_name = os.getenv("ELASTICSEARCH_INDEX", "idx_prompting_agent_v2")
    print(f"DEBUG: Menggunakan index bernama -> {index_name}")
    mapping_path = Path(__file__).parent.parent / "data" / "mapping.json"
    
    # Validate mapping file
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping file not found at {mapping_path}")
    
    with open(mapping_path) as f:
        mapping = json.load(f)
    
    # Check connection
    try:
        es.ping()
        print("✓ Connected to Elasticsearch")
    except exceptions.ConnectionError as e:
        raise ConnectionError(f"Failed to connect to Elasticsearch: {e}")
    
    # Handle existing index
    if es.indices.exists(index=index_name):
        if force:
            print(f"  Index '{index_name}' exists. Deleting for reset...")
            es.indices.delete(index=index_name)
            print("✓ Index deleted")
        else:
            print(f"ℹ  Index '{index_name}' already exists. Use --force to reset.")
            return
    
    # Create index
    es.indices.create(index=index_name, body=mapping)
    print(f" Index '{index_name}' created successfully with dense_vector mapping!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize Elasticsearch index")
    parser.add_argument("--force", action="store_true", help="Force delete existing index")
    args = parser.parse_args()
    
    try:
        init_index(force=args.force)
    except Exception as e:
        print(f" Failed to initialize index: {e}")
        exit(1)