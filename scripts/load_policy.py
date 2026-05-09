# Day-0 ingestion. Run this once per benefits document.
# It chunks the doc, embeds each chunk with Titan, and stores in S3 Vectors
# with the policy name as metadata so the query Lambda can filter by plan.
#
# Usage:
#   cp .env.example .env   # fill in AWS_ACCOUNT_ID etc.
#   export $(grep -v '^#' .env | xargs)
#   python scripts/load_policy.py --file docs/ppo_plus_2024.txt --policy PPO_PLUS
#   python scripts/load_policy.py --file docs/hmo_select_2024.txt --policy HMO_SELECT

import argparse
import json
import os
import boto3

# Load .env from the project root if present (no extra dependencies needed).
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

_account_id = os.environ.get("AWS_ACCOUNT_ID")
if not _account_id:
    raise EnvironmentError("AWS_ACCOUNT_ID is not set. Copy .env.example to .env and fill in your values.")

VECTOR_BUCKET = f"benefits-vectors-{_account_id}"
VECTOR_INDEX  = os.environ.get("VECTOR_INDEX", "benefits-index")
REGION        = os.environ.get("AWS_REGION", "us-east-1")
EMBED_MODEL   = os.environ.get("EMBED_MODEL", "amazon.titan-embed-text-v2:0")

bedrock   = boto3.client("bedrock-runtime", region_name=REGION)
s3vectors = boto3.client("s3vectors", region_name=REGION)


def embed(text: str) -> list[float]:
    response = bedrock.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def chunk_text(text: str, max_chars: int = 500) -> list[str]:
    # Paragraph-based chunking. Benefits docs are naturally structured this way —
    # each benefit category is its own paragraph block. Merging short paragraphs
    # keeps chunks above the minimum size needed for meaningful embeddings.
    paragraphs    = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""

    for para in paragraphs:
        if len(current) + len(para) + 1 <= max_chars:
            current = (current + " " + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            current = para

    if current:
        chunks.append(current)

    return chunks


def load(filepath: str, policy: str):
    text   = open(filepath, encoding="utf-8").read()
    chunks = chunk_text(text)

    print(f"Loading {len(chunks)} chunks for policy {policy}...")

    # S3 Vectors PutVectors accepts up to 500 vectors per call.
    # Batch in groups of 100 to be safe.
    BATCH = 100
    for batch_start in range(0, len(chunks), BATCH):
        batch       = chunks[batch_start:batch_start + BATCH]
        vectors_payload = []

        for i, chunk in enumerate(batch):
            idx       = batch_start + i
            embedding = embed(chunk)

            vectors_payload.append({
                # key must be unique within the index
                "key": f"{policy}_chunk_{idx:04d}",
                "data": {"float32": embedding},
                # metadata is how the query Lambda filters by policy
                # and retrieves the chunk text without a separate database call
                "metadata": {
                    "policy":      policy,
                    "chunk":       chunk,
                    "chunk_index": idx,
                    "source":      filepath,
                },
            })

        s3vectors.put_vectors(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            vectors=vectors_payload,
        )
        print(f"  stored chunks {batch_start}–{batch_start + len(batch) - 1}")

    print(f"Done. {len(chunks)} chunks stored for {policy}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",   required=True, help="path to the benefits .txt file")
    parser.add_argument("--policy", required=True, help="policy name e.g. PPO_PLUS")
    args = parser.parse_args()
    load(args.file, args.policy.upper())