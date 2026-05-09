# lambda/query/query.py
#
# The full query flow in one handler:
#   1. Apply Bedrock Guardrails to the user's input
#   2. Embed the (clean) question with Titan Embed V2
#   3. Query S3 Vectors for the top 3 most similar chunks for this policy
#   4. Ask Claude with those chunks as context
#   5. Guardrails are applied inline during the Claude call via guardrailIdentifier
#
# No dependencies beyond boto3 — already in the Lambda runtime.
# No in-memory cosine similarity — S3 Vectors does that natively.

import json
import os
import boto3

VECTOR_BUCKET = os.environ["VECTOR_BUCKET_NAME"]
VECTOR_INDEX = os.environ["VECTOR_INDEX_NAME"]
GUARDRAIL_ID = os.environ["GUARDRAIL_ID"]
GUARDRAIL_VER = os.environ["GUARDRAIL_VERSION"]
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")

bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)
s3vectors = boto3.client("s3vectors", region_name=REGION)

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
# CLAUDE_MODEL = "anthropic.claude-3-haiku-20240307-v1:0"
CLAUDE_MODEL = "amazon.nova-micro-v1:0"


def check_guardrail_input(text: str) -> tuple[bool, str]:
    # Run the user's question through Bedrock Guardrails before doing anything else.
    # If it's off-topic or contains PII, we handle it here.
    # Blocked means off-topic — return False so we stop immediately.
    # Anonymized means PII was found and replaced — the cleaned text comes back.
    response = bedrock_runtime.apply_guardrail(
        guardrailIdentifier=GUARDRAIL_ID,
        guardrailVersion=GUARDRAIL_VER,
        source="INPUT",
        content=[{"text": {"text": text}}],
    )

    action = response.get("action", "NONE")

    if action == "GUARDRAIL_INTERVENED":
        # Check if it was blocked (off-topic) or just anonymized (PII stripped)
        outputs = response.get("outputs", [])
        if outputs:
            cleaned = outputs[0].get("text", text)
        else:
            # No output = full block, not just anonymization
            return False, ""
        return True, cleaned

    return True, text


def embed(text: str) -> list[float]:
    # Titan Embed V2 — same model used during ingestion.
    # Must be the same model or the vector spaces won't be comparable.
    response = bedrock_runtime.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def retrieve_chunks(
    question_vector: list[float], policy: str, top_k: int = 3
) -> list[str]:
    # S3 Vectors does the similarity search natively.
    # We filter by policy metadata so we only search within that plan's chunks.
    # No scan, no loading everything into memory, no cosine math on our side.
    response = s3vectors.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        queryVector={"float32": question_vector},
        topK=top_k,
        returnMetadata=True,
        returnDistance=True,
    )

    vectors = response.get("vectors", [])
    if not vectors:
        return []

    # The chunk text was stored as metadata during ingestion in load_policy.py
    return [v["metadata"]["chunk"] for v in vectors if "chunk" in v.get("metadata", {})]


def ask_nova(chunks: list[str], question: str) -> str:
    context = "\n\n---\n\n".join(chunks)

    response = bedrock_runtime.invoke_model(
        modelId=CLAUDE_MODEL,
        guardrailIdentifier=GUARDRAIL_ID,
        guardrailVersion=GUARDRAIL_VER,
        body=json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": f"You are a benefits assistant. Answer using only the provided context. If the answer is not in the context, say so.\n\nContext:\n{context}\n\nQuestion: {question}"
                            }
                        ],
                    }
                ],
                "inferenceConfig": {"max_new_tokens": 512},
            }
        ),
        contentType="application/json",
        accept="application/json",
    )
    body = json.loads(response["body"].read())
    return body["output"]["message"]["content"][0]["text"]


def handle(event, context):
    # API Gateway sends the body as a JSON string — always parse it.
    try:
        body = json.loads(event.get("body") or "{}")
        question = body.get("question", "").strip()
        policy = body.get("policy", "").strip().upper()
    except (json.JSONDecodeError, AttributeError):
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "invalid request body"}),
        }

    if not question:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "question is required"}),
        }

    if not policy:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "policy is required — e.g. PPO_PLUS"}),
        }

    print(f"policy={policy} question={question[:80]}")

    # Step 1 — guardrail check on input
    allowed, clean_question = check_guardrail_input(question)
    if not allowed:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "answer": (
                        "I can only answer questions about your employee benefits plan. "
                        "Please ask about health insurance, dental, vision, 401k, or PTO."
                    )
                }
            ),
        }

    # Step 2 — embed the clean question
    question_vector = embed(clean_question)

    # Step 3 — retrieve top matching chunks from S3 Vectors
    chunks = retrieve_chunks(question_vector, policy, top_k=3)
    if not chunks:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": f"no documents found for policy '{policy}'"}),
        }

    # Step 4 — ask Claude (guardrails applied inline on the output)
    answer = ask_nova(chunks, clean_question)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "policy": policy,
                "question": question,
                "answer": answer,
                "chunks_used": len(chunks),
            }
        ),
    }
