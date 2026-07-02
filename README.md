# AI Code Review & Technical Debt Agent

> An AI-powered GitHub bot that reviews PRs across Security, Architecture, Test Coverage, and Technical Debt dimensions using a fine-tuned local model with Claude escalation — demonstrating multi-agent orchestration, hybrid cost engineering, and time-series debt tracking.

---

## Architecture Overview

```
GitHub PR Event
      │
      ▼
┌─────────────────┐     HMAC verify      ┌──────────────────┐
│  FastAPI        │ ──────────────────▶  │   Celery Worker  │
│  /webhooks/gh   │     enqueue job       │                  │
└─────────────────┘                       │  LangGraph DAG   │
                                          │  ┌─────────────┐ │
                                          │  │ Security    │ │
                                          │  │ Arch        │ │ ──▶ GitHub PR Comments
                                          │  │ Test-Cover  │ │ ──▶ Check Run Status
                                          │  │ Debt Score  │ │
                                          │  └─────────────┘ │
                                          └──────────────────┘
                                                   │
                                     ┌─────────────┼──────────────┐
                                     ▼             ▼              ▼
                                  Postgres      ChromaDB        Redis
                                (TimescaleDB)  (RAG store)    (queue/cache)
```

## Tech Stack

| Layer | Technology |
|---|---|
| API / Webhook | FastAPI + Uvicorn |
| Async Jobs | Celery + Redis |
| Agent Orchestration | LangGraph |
| Primary LLM | Claude Sonnet (escalation) + fine-tuned Qwen2.5-Coder-7B (bulk) |
| Static Analysis | tree-sitter, libcst, unidiff |
| Code Graph | Neo4j (Phase 3+) |
| RAG | ChromaDB + sentence-transformers |
| Relational DB | PostgreSQL 16 + TimescaleDB |
| Observability | Prometheus + Grafana + Loki |
| Frontend | Next.js 16 + Recharts (Phase 5+) |

---

## Quick Start

### 1. Prerequisites

- Docker Desktop (Windows with WSL2 backend, or Linux/macOS)
- Node.js 20+ (for smee.io tunnel client)
- Python 3.11+ (for local development outside Docker)

### 2. Clone & Configure

```bash
git clone https://github.com/YOUR_USERNAME/code-review-agent.git
cd code-review-agent

# Copy env template and fill in your values
cp .env.example .env
```

See [`docs/github-app-setup.md`](docs/github-app-setup.md) to create the GitHub App and get your credentials.

### 3. Start Infrastructure

```bash
# Start Postgres+TimescaleDB, Redis, and ChromaDB
docker-compose up -d db redis chroma

# Verify all services are healthy
docker-compose ps
```

The schema is applied automatically on first Postgres startup via `docker-entrypoint-initdb.d`.

### 4. Start the Webhook Tunnel

See [`docs/webhook-tunnel-setup.md`](docs/webhook-tunnel-setup.md).

```bash
# smee.io (recommended)
npx smee-client --url https://smee.io/YOUR_CHANNEL --target http://localhost:8000/webhooks/github
```

### 5. Start the Backend

```bash
# Option A: Docker (recommended)
docker-compose up backend worker

# Option B: Local Python
cd backend
pip install -e ".[dev]"
uvicorn app.main:app --reload &
celery -A app.worker.celery_app worker --loglevel=info -Q pr_review
```

### 6. Verify

```bash
curl http://localhost:8000/health
# → {"status": "ok"}
```

Open a PR on the GitHub repo where your app is installed → watch the backend logs for:
```
INFO  webhook_received  repo=owner/repo pr_number=1 action=opened
INFO  webhook_task_enqueued  task_id=pr-review-...
```

---

## Project Structure

```
code-review-agent/
├── backend/              # FastAPI app + Celery worker
│   ├── app/
│   │   ├── main.py       # FastAPI routes (/health, /webhooks/github)
│   │   ├── config.py     # pydantic-settings Settings
│   │   ├── security.py   # HMAC-SHA256 webhook verification
│   │   ├── worker.py     # Celery app factory
│   │   ├── tasks.py      # Celery task definitions
│   │   └── db/
│   │       ├── base.py   # SQLAlchemy async engine + session
│   │       └── models.py # ORM models (repos, PRs, reviews, findings, debt_scores)
│   ├── migrations/       # Alembic migrations
│   ├── scripts/
│   │   └── init_db.sql   # Reference schema (auto-run on first container start)
│   ├── Dockerfile
│   └── pyproject.toml
├── agents/               # LangGraph orchestrator + agent nodes (Phase 1+)
│   ├── schemas.py        # PRContext, ChangedFile, Finding (Pydantic v2)
│   └── __init__.py
├── dashboard/            # Next.js dashboard (Phase 5+)
├── docs/
│   ├── github-app-setup.md
│   └── webhook-tunnel-setup.md
├── secrets/              # .gitignored — place github-app.pem here
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Development Phases

| Phase | Focus | Status |
|---|---|---|
| **0** | Setup: Docker, webhook receiver, DB schema | ✅ Done |
| **1** | Ingestion: HMAC, Celery, diff fetch, tree-sitter AST | ✅ Done |
| **2** | First agent: Security Agent + inline PR comments | ✅ Done |
| **3** | All 4 agents + Neo4j blast-radius graph | 🔜 Next |
| **4** | RAG: repo conventions retrieval into agent prompts | ⏳ |
| **5** | TimescaleDB debt tracking + Next.js dashboard | ⏳ |
| **6** | Fine-tuning + vLLM + hybrid cost routing | ⏳ |
| **7** | Observability: Prometheus + Grafana + Loki | ⏳ |
| **8** | Validation on real PRs + resume metrics | ⏳ |

---

## License

MIT
