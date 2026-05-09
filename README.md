# Benefits Q&A — Amazon Bedrock + AWS CDK

A production-ready RAG (Retrieval-Augmented Generation) system that lets employees ask natural-language questions about their health insurance benefits plans and receive grounded, policy-specific answers — deployed entirely through reusable AWS CDK infrastructure.

---

## Business Use Case

HR and benefits teams maintain dense, plan-specific documents (PPO, HMO, dental, vision) that employees rarely read and constantly misunderstand. Every open-enrollment season and life event generates a wave of repetitive support tickets: *"Does my plan cover urgent care?"*, *"What is my deductible for out-of-network?"*, *"Is my spouse covered under HMO Select?"*

This system replaces that support burden with a self-service Q&A assistant that:

- Answers **only** health-benefits questions — any off-topic query is blocked at the guardrail layer before it reaches the LLM.
- Stays **grounded in the actual policy text** — the LLM never generates from general knowledge; it reasons only over retrieved chunks from the uploaded plan document.
- Filters answers **per plan** — an employee on PPO Plus only gets answers from PPO Plus chunks; HMO Select is a separate embedding namespace.
- **Masks PII automatically** — names, SSNs, phone numbers, and addresses in any question are anonymised before they touch the model.
- Is **fully managed** — no vector database to provision, no GPU cluster to maintain; S3 Vectors, Bedrock, and Lambda handle everything.

### Supported Plans (out of the box)

| Plan key | Document |
|---|---|
| `PPO_PLUS` | `docs/ppo_plus_2024.txt` |
| `HMO_SELECT` | `docs/hmo_select_2024.txt` |

---

## Stack

| Layer | Service | Purpose |
|---|---|---|
| IaC | AWS CDK (TypeScript) | Declare and deploy all infrastructure as code |
| Vector store | Amazon S3 Vectors | Serverless vector index — no extra infra |
| Embedding model | Amazon Titan Embed V2 | 1024-dim embeddings, consistent at ingest and query time |
| LLM | Amazon Nova Micro | Fast, low-cost inference for benefits Q&A |
| Safety | Amazon Bedrock Guardrails | Topic block + PII anonymisation on input and output |
| Compute | AWS Lambda (Python 3.12) | Stateless query handler wired to Bedrock and S3 Vectors |
| API | Amazon API Gateway | Public HTTPS endpoint: `POST /ask` |
| Ingestion | Python script (`load_policy.py`) | One-time day-0 document loader |

---

## Diagrams

### 1. System Architecture

```mermaid
graph TB
    subgraph Client
        U[Employee / HR Portal]
    end

    subgraph API["API Layer"]
        APIGW[API Gateway\nPOST /ask]
    end

    subgraph Compute["Compute Layer"]
        LQ[Lambda\nquery.py]
    end

    subgraph Safety["Bedrock Guardrails"]
        GI[Input Check\nTopic Block + PII Mask]
        GO[Output Check\nTopic Block + PII Mask]
    end

    subgraph Bedrock["Amazon Bedrock"]
        TE[Titan Embed V2\n1024-dim embeddings]
        NM[Nova Micro\nLLM inference]
    end

    subgraph Storage["Vector Store"]
        VB[S3 Vector Bucket\nbenefits-vectors-ACCOUNT]
        VI[Vector Index\nbenefits-index\ncosine · float32 · 1024-dim]
    end

    subgraph Ingestion["Day-0 Ingestion"]
        SC[load_policy.py]
        DOC[Policy Documents\nppo_plus_2024.txt\nhmo_select_2024.txt]
    end

    U -->|POST question + policy| APIGW
    APIGW --> LQ
    LQ --> GI
    GI -->|clean question| TE
    TE -->|query vector| VI
    VI -->|top-3 chunks| LQ
    LQ --> NM
    NM --> GO
    GO -->|safe answer| LQ
    LQ -->|JSON response| APIGW
    APIGW --> U

    DOC --> SC
    SC --> TE
    TE -->|embeddings + metadata| VI
    VI --> VB
```

---

### 2. Infrastructure Deployment Flow

```mermaid
flowchart TD
    A([Developer]) --> B[cdk bootstrap\none-time per account/region]
    B --> C[npm run build\ntsc compiles TypeScript CDK]
    C --> D[cdk synth\ngenerates CloudFormation template]
    D --> E{Review\nchangeset?}
    E -->|yes| F[cdk diff\npreview resource delta]
    F --> G[cdk deploy BenefitsStack]
    E -->|no| G
    G --> H[CloudFormation creates resources in order]

    subgraph Resources["Resources Created — dependency order"]
        R1[S3 Vector Bucket\nbenefits-vectors-ACCOUNT]
        R2[S3 Vector Index\nbenefits-index]
        R3[Bedrock Guardrail\n+ GuardrailVersion snapshot]
        R4[Lambda IAM Role\ns3vectors + bedrock permissions]
        R5[Lambda Function\nQueryFn · Python 3.12 · 512 MB]
        R6[API Gateway\nbenefits-explainer · POST /ask]
        R1 --> R2
        R3 --> R4
        R2 --> R5
        R4 --> R5
        R5 --> R6
    end

    H --> Resources
    Resources --> I[CDK Outputs\nApiUrl · GuardrailId · VectorBucketName]
    I --> J([Stack ready — run load_policy.py])
```

---

### 3. Document Ingestion Flow

```mermaid
sequenceDiagram
    actor Dev as Developer
    participant S as load_policy.py
    participant BE as Bedrock\nTitan Embed V2
    participant SV as S3 Vectors\nbenefits-index

    Dev->>S: python load_policy.py\n--file docs/ppo_plus_2024.txt\n--policy PPO_PLUS

    S->>S: Read file, split on double newline\nmerge short paragraphs up to 500 chars
    Note over S: N chunks produced\ne.g. 42 chunks for PPO Plus

    loop For each batch of 100 chunks
        loop For each chunk in batch
            S->>BE: invoke_model Titan Embed V2\ninputText = chunk text
            BE-->>S: embedding 1024 float32 values
            S->>S: build vector payload\nkey: PPO_PLUS_chunk_0001\nmetadata: policy, chunk, index, source
        end
        S->>SV: put_vectors batch\nvectorBucketName, indexName, vectors
        SV-->>S: 200 OK
    end

    S->>Dev: Done. 42 chunks stored for PPO_PLUS
```

---

### 4. Query-time RAG Flow

```mermaid
sequenceDiagram
    actor User
    participant GW as API Gateway\nPOST /ask
    participant LQ as Lambda\nquery.py
    participant GR as Bedrock Guardrails
    participant TE as Bedrock\nTitan Embed V2
    participant SV as S3 Vectors\nbenefits-index
    participant NM as Bedrock\nNova Micro

    User->>GW: POST /ask\n{question, policy}
    GW->>LQ: invoke

    Note over LQ: Step 1 — validate inputs\nquestion and policy required

    LQ->>GR: apply_guardrail source=INPUT\nuser question
    GR-->>LQ: action + cleaned text

    alt Blocked — off-topic
        LQ-->>GW: 200 canned refusal message
        GW-->>User: I can only answer benefits questions
    else Anonymized — PII stripped
        Note over LQ: PII replaced, use cleaned text
    else Pass
        Note over LQ: Original text, proceed
    end

    Note over LQ: Step 2 — embed the clean question

    LQ->>TE: invoke_model Titan Embed V2\nclean question
    TE-->>LQ: question_vector 1024 floats

    Note over LQ: Step 3 — retrieve top-3 similar chunks

    LQ->>SV: query_vectors\ntopK=3, returnMetadata=true
    SV-->>LQ: key, distance, metadata chunk

    Note over LQ: Step 4 — generate answer with Nova Micro\nGuardrails applied inline on output

    LQ->>NM: invoke_model Nova Micro\ncontext = 3 chunks, question\nguardrailIdentifier inline
    NM-->>LQ: generated answer with guardrailed output

    LQ-->>GW: 200 {policy, question, answer, chunks_used}
    GW-->>User: JSON response
```

---

### 5. Guardrails Decision Flow

```mermaid
flowchart TD
    A([User question arrives]) --> B[apply_guardrail\nsource=INPUT]

    B --> C{action?}

    C -->|NONE - no intervention| D[Pass through\nuse original text]
    C -->|GUARDRAIL_INTERVENED| E{outputs present?}

    E -->|No outputs - full block| F[return False\nstop processing]
    E -->|Output present - PII anonymised| G[return True\nuse cleaned text]

    F --> H[Lambda returns\ncanned refusal 200 OK]
    D --> I[Embed clean question\nTitan Embed V2]
    G --> I

    I --> J[Retrieve chunks\nS3 Vectors top-3]
    J --> K[invoke Nova Micro\nwith guardrailIdentifier inline]

    K --> L{Output guardrail\nfires?}
    L -->|Blocked| M[blockedOutputsMessaging\nreturned to user]
    L -->|Safe| N[Return answer\nto user]

    style F fill:#c0392b,color:#fff
    style H fill:#c0392b,color:#fff
    style M fill:#c0392b,color:#fff
    style N fill:#27ae60,color:#fff
    style D fill:#2980b9,color:#fff
    style G fill:#f39c12,color:#fff
```

---

### 6. Policy Filtering Flow

```mermaid
flowchart LR
    subgraph Ingest["Ingestion — metadata written per chunk"]
        P1[PPO_PLUS chunks\nkey: PPO_PLUS_chunk_0001\nmetadata.policy = PPO_PLUS]
        P2[HMO_SELECT chunks\nkey: HMO_SELECT_chunk_0001\nmetadata.policy = HMO_SELECT]
    end

    subgraph Index["S3 Vector Index — benefits-index\ncosine similarity · 1024-dim float32"]
        VI[(All vectors from all policies\nco-located in one index)]
    end

    subgraph Query["Query — policy passed in request body"]
        QV[question_vector]
        POL{policy = PPO_PLUS}
    end

    subgraph Result["Retrieved Chunks"]
        RC[top-3 PPO_PLUS chunks only\nno HMO_SELECT bleed]
    end

    P1 -->|put_vectors| VI
    P2 -->|put_vectors| VI
    QV --> POL
    POL -->|query_vectors\nfiltered client-side\nvia metadata.policy| VI
    VI --> RC

    style RC fill:#27ae60,color:#fff
```

---

### 7. End-to-End Data Flow

```mermaid
flowchart LR
    subgraph Raw["Raw Data"]
        TXT[benefits .txt file\nplain text paragraphs]
    end

    subgraph Chunk["Chunking"]
        C1[chunk 1\nup to 500 chars]
        C2[chunk 2\nup to 500 chars]
        CN[chunk N\nup to 500 chars]
    end

    subgraph Embed["Embedding — Titan V2"]
        E1["0.021, -0.184, ...\n1024 float32"]
        E2["0.114, 0.033, ...\n1024 float32"]
        EN["0.023, -0.183, ...\n1024 float32"]
    end

    subgraph Store["S3 Vector Index"]
        VI[(key + embedding + metadata)]
    end

    subgraph QueryEmbed["Query Embedding"]
        QE["question_vector\n0.019, -0.180, ...\n1024 float32"]
    end

    subgraph Retrieve["Cosine Similarity Search"]
        T1[top-1 chunk\ndist=0.05]
        T2[top-2 chunk\ndist=0.12]
        T3[top-3 chunk\ndist=0.18]
    end

    subgraph Generate["Nova Micro — Generation"]
        CTX[context = top-3 chunk texts\nconcatenated with separators]
        ANS[grounded answer\nbased only on context]
    end

    TXT --> C1 & C2 & CN
    C1 --> E1
    C2 --> E2
    CN --> EN
    E1 & E2 & EN --> VI
    QE -->|query vector| VI
    VI -->|similarity search| T1 & T2 & T3
    T1 & T2 & T3 --> CTX
    CTX --> ANS
```

---

### 8. Teardown Flow

```mermaid
flowchart TD
    A([Developer]) --> B[Empty S3 Vectors index\nbefore destroy]
    B --> C{S3 Vectors auto-delete\non stack destroy?}
    C -->|No - manual step| D[aws s3vectors delete-index\nthen delete-vector-bucket]
    D --> E[cdk destroy BenefitsStack]
    C -->|Yes if retention=DESTROY| E

    E --> F[CloudFormation deletes\nin reverse dependency order]

    subgraph Destroy["Resources deleted — reverse order"]
        D1[API Gateway]
        D2[Lambda Function]
        D3[Lambda IAM Role]
        D4[Bedrock GuardrailVersion]
        D5[Bedrock Guardrail]
        D6[S3 Vector Index]
        D7[S3 Vector Bucket]
        D1 --> D2 --> D3
        D4 --> D5
        D6 --> D7
    end

    F --> Destroy
    Destroy --> G[CDK bootstrap stack\noptionally destroy separately]
    G --> H([Account clean - no orphaned resources])

    style H fill:#27ae60,color:#fff
    style D fill:#f39c12,color:#fff
```

---

## Quick Start

### Prerequisites

- AWS account with Bedrock model access enabled for `amazon.titan-embed-text-v2:0` and `amazon.nova-micro-v1:0`
- Node.js 18+ and Python 3.12+
- AWS CDK CLI: `npm install -g aws-cdk`

### Deploy

```bash
# 1. Install CDK dependencies
npm install

# 2. Bootstrap CDK (once per account/region)
cdk bootstrap

# 3. Build TypeScript
npm run build

# 4. Deploy
cdk deploy BenefitsStack
```

### Ingest Policy Documents

```bash
# Copy and fill in your AWS account details
cp .env.example .env

# Activate the Python virtual environment
source .venv/bin/activate

# Ingest both plans
python scripts/load_policy.py --file docs/ppo_plus_2024.txt  --policy PPO_PLUS
python scripts/load_policy.py --file docs/hmo_select_2024.txt --policy HMO_SELECT
```

### Query

```bash
# Replace API_URL with the ApiUrl value from CDK outputs
curl -s -X POST $API_URL/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is my annual deductible?", "policy": "PPO_PLUS"}' | jq
```

Expected response:

```json
{
  "policy": "PPO_PLUS",
  "question": "What is my annual deductible?",
  "answer": "Your PPO Plus annual deductible is ...",
  "chunks_used": 3
}
```

### Teardown

```bash
cdk destroy BenefitsStack
```


---

## Project Structure

```
ts-benefits-cdk/
├── app.ts                      # CDK app entry point
├── stacks/
│   └── benefitsStack.ts        # All infrastructure — S3 Vectors, Guardrails, Lambda, API GW
├── lambda/
│   └── query/
│       └── query.py            # RAG handler: guardrail → embed → retrieve → generate
├── scripts/
│   └── load_policy.py          # Day-0 ingestion: chunk → embed → put_vectors
├── docs/
│   ├── ppo_plus_2024.txt       # PPO Plus benefits document
│   └── hmo_select_2024.txt     # HMO Select benefits document
├── .env.example                # Environment variable template
└── cdk.json                    # CDK app configuration
```

---

## Guardrail Policy

| Rule | Configuration |
|---|---|
| Blocked topics | Anything not about health insurance benefits (finance, medical diagnosis, legal, coding, general knowledge) |
| PII anonymised | `NAME`, `EMAIL`, `PHONE`, `ADDRESS`, `US_SOCIAL_SECURITY_NUMBER`, `CREDIT_DEBIT_CARD_NUMBER` |
| Blocked input message | `"I can only answer questions about your health insurance benefits plan."` |
| Blocked output message | `"I was unable to generate a safe response. Please rephrase your question."` |
| Applied at | Input via explicit `apply_guardrail` call and output via inline `guardrailIdentifier` on `invoke_model` |
