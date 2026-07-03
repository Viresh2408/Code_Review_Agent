"""
Repository conventions indexing and retrieval module.

Extracts README, documentation files, and recent PR diffs from a local git repository,
chunks them, embeds them using sentence-transformers, and stores them in ChromaDB.
Provides retrieval of relevant conventions during PR analysis.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import chromadb
import structlog
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from filelock import FileLock
from github import GithubIntegration

from app.config import get_settings

logger = structlog.get_logger(__name__)


# ── Embedding Model Cache (Scalable Singleton Pattern) ────────────────────────

_model_cache: dict[str, Any] = {}

def _get_embedding_model(model_name: str = "all-MiniLM-L6-v2") -> Any:
    """
    Get the sentence-transformers model instance from memory cache.
    Loads it lazily on the correct device (GPU/MPS/CPU).
    """
    if model_name not in _model_cache:
        import torch
        from sentence_transformers import SentenceTransformer
        device = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
        logger.info("loading_sentence_transformer_model", model_name=model_name, device=device)
        _model_cache[model_name] = SentenceTransformer(model_name, device=device)
    return _model_cache[model_name]


class SentenceTransformerEmbeddingFunction(EmbeddingFunction):
    """
    Custom wrapper around sentence-transformers library to generate
    embeddings inside ChromaDB. Uses model caching for extreme performance.
    """
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name

    def __call__(self, input: Documents) -> Embeddings:
        model = _get_embedding_model(self.model_name)
        embeddings = model.encode(input, convert_to_numpy=True)
        return embeddings.tolist()


# ── Database Connection Cache (Scalable Connection Pooling) ───────────────────

_chroma_client: chromadb.ClientAPI | None = None
_is_persistent_local: bool = False

def _get_chroma_client() -> chromadb.ClientAPI:
    """
    Initialize and return a ChromaDB client. Caches connection for scalability.
    Tries HTTP client first (Docker), falls back to PersistentClient (local sqlite).
    """
    global _chroma_client, _is_persistent_local
    if _chroma_client is not None:
        return _chroma_client

    settings = get_settings()
    try:
        client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        # Verify connection works
        client.heartbeat()
        logger.debug("chromadb_http_connection_successful", host=settings.chroma_host, port=settings.chroma_port)
        _chroma_client = client
        _is_persistent_local = False
        return client
    except Exception as exc:
        logger.warning(
            "chromadb_http_connection_failed_falling_back_to_persistent_client",
            host=settings.chroma_host,
            port=settings.chroma_port,
            error=str(exc)
        )
        # Local fallback
        client = chromadb.PersistentClient(path="./chroma_data")
        _chroma_client = client
        _is_persistent_local = True
        return client


def _get_collection(client: chromadb.ClientAPI) -> chromadb.Collection:
    """
    Get or create the repo_conventions collection with the custom embedding function.
    """
    emb_fn = SentenceTransformerEmbeddingFunction()
    return client.get_or_create_collection(
        name="repo_conventions",
        embedding_function=emb_fn
    )


# ── Text & Diff Chunkers (Guaranteed Overlap) ─────────────────────────────────

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """
    Split text into chunks of maximum size, respecting line boundaries.
    Maintains a 200-character overlap to avoid splitting conventions.
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    lines = text.splitlines(keepends=True)
    current_chunk = []
    current_len = 0

    for line in lines:
        if current_len + len(line) <= chunk_size:
            current_chunk.append(line)
            current_len += len(line)
        else:
            if current_chunk:
                chunks.append("".join(current_chunk))
            # Handle single lines longer than chunk_size
            if len(line) > chunk_size:
                start = 0
                while start < len(line):
                    chunks.append(line[start:start+chunk_size])
                    start += chunk_size - overlap
                current_chunk = []
                current_len = 0
            else:
                current_chunk = [line]
                current_len = len(line)

    if current_chunk:
        chunks.append("".join(current_chunk))
    return chunks


def chunk_pr_diff(diff_text: str, chunk_size: int = 1500, overlap: int = 200) -> list[str]:
    """
    Split a PR diff into chunks, keeping individual file diffs together where possible.
    Maintains a 200-character overlap to avoid splitting conventions.
    """
    file_diffs = []
    current_file_diff = []

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if current_file_diff:
                file_diffs.append("".join(current_file_diff))
            current_file_diff = [line]
        else:
            current_file_diff.append(line)

    if current_file_diff:
        file_diffs.append("".join(current_file_diff))

    chunks = []
    for fd in file_diffs:
        if len(fd) <= chunk_size:
            chunks.append(fd)
        else:
            start = 0
            while start < len(fd):
                chunks.append(fd[start:start+chunk_size])
                start += chunk_size - overlap
    return chunks


# ── PR Commits Ingestion ──────────────────────────────────────────────────────

def get_recent_pr_commits(repo_path: str, max_prs: int = 20) -> list[dict[str, str]]:
    """
    Extract hashes and parent commit info for the last max_prs merged PRs using git log.
    Identifies PRs by:
      - Merge commits (multiple parents)
      - Commit message ending with " (#<digits>)" or containing "Merge pull request #"
    """
    try:
        # Run git log format: SHA|PARENTS|SUBJECT
        cmd = ["git", "log", "-n", "300", "--format=%H|%P|%s"]
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=True)
    except Exception as exc:
        logger.warning("git_log_failed_not_a_git_repository", repo_path=repo_path, error=str(exc))
        return []

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, parents_str, subject = parts
        parents = parents_str.strip().split()

        is_pr = False
        if len(parents) > 1:
            is_pr = True
        elif "Merge pull request #" in subject:
            is_pr = True
        elif " (#" in subject and subject.strip().endswith(")"):
            if re.search(r" \(#\d+\)$", subject.strip()):
                is_pr = True

        if is_pr and parents:  # Needs at least one parent for diffing
            commits.append({
                "sha": sha,
                "parent": parents[0],  # First parent (target branch base)
                "subject": subject
            })
            if len(commits) == max_prs:
                break
    return commits


def _fetch_diff_concurrent(repo_path: str, parent: str, sha: str) -> str | None:
    """Helper function to fetch git diff for a commit."""
    try:
        cmd = ["git", "diff", parent, sha]
        res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=True)
        return res.stdout
    except Exception as exc:
        logger.error("failed_to_extract_pr_diff_concurrent", sha=sha, error=str(exc))
        return None


# ── Core Ingestion Pipeline ───────────────────────────────────────────────────

def index_repo_conventions(repo_id: str, repo_path: str) -> None:
    """
    Extract README, docs/adr directories, and up to 20 recently merged PR diffs from the repo_path,
    chunk and embed them into the repo_conventions collection.
    """
    path = Path(repo_path)
    if not path.is_dir():
        raise FileNotFoundError(f"Repository directory does not exist: {repo_path}")

    logger.info("indexing_repo_conventions_started", repo_id=repo_id, repo_path=repo_path)

    documents = []
    metadatas = []
    ids = []

    # 1. Extract README (Skip files > 1MB)
    readme_path = None
    for f in path.iterdir():
        if f.is_file() and f.name.lower().startswith("readme"):
            readme_path = f
            break

    if readme_path:
        try:
            if readme_path.stat().st_size <= 1024 * 1024:
                content = readme_path.read_text(encoding="utf-8", errors="ignore")
                chunks = chunk_text(content, chunk_size=1000, overlap=200)
                for idx, chunk in enumerate(chunks):
                    documents.append(chunk)
                    metadatas.append({
                        "repo_id": repo_id,
                        "type": "readme",
                        "source": readme_path.name
                    })
                    ids.append(f"{repo_id}_readme_{idx}")
                logger.info("indexed_readme", chunks_count=len(chunks))
            else:
                logger.warning("skipping_excessive_readme_file", path=str(readme_path))
        except Exception as exc:
            logger.error("failed_to_read_readme", path=str(readme_path), error=str(exc))

    # 2. Extract Docs and ADR (Skip files > 1MB)
    docs_files = []
    for dir_name in ("docs", "adr"):
        dir_path = path / dir_name
        if dir_path.is_dir():
            for p in dir_path.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".md", ".txt", ".rst", ".html", ".adr", ""):
                    if p.stat().st_size <= 1024 * 1024:
                        docs_files.append(p)
                    else:
                        logger.warning("skipping_large_documentation_file", path=str(p), size=p.stat().st_size)

    for p in docs_files:
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
            rel_path = p.relative_to(path).as_posix()
            chunks = chunk_text(content, chunk_size=1000, overlap=200)
            for idx, chunk in enumerate(chunks):
                documents.append(chunk)
                metadatas.append({
                    "repo_id": repo_id,
                    "type": "doc",
                    "source": rel_path
                })
                ids.append(f"{repo_id}_doc_{rel_path.replace('/', '_').replace('.', '_')}_{idx}")
        except Exception as exc:
            logger.error("failed_to_read_doc", path=str(p), error=str(exc))

    if docs_files:
        logger.info("indexed_docs_and_adrs", files_count=len(docs_files))

    # 3. Extract PR diffs concurrently (ThreadPoolExecutor)
    pr_commits = get_recent_pr_commits(repo_path, max_prs=20)
    if pr_commits:
        logger.info("fetching_pr_diffs_concurrently", count=len(pr_commits))
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                (pr_comm, executor.submit(_fetch_diff_concurrent, repo_path, pr_comm["parent"], pr_comm["sha"]))
                for pr_comm in pr_commits
            ]
            for pr_comm, fut in futures:
                diff_text = fut.result()
                if not diff_text or not diff_text.strip():
                    continue
                sha = pr_comm["sha"]
                subject = pr_comm["subject"]
                chunks = chunk_pr_diff(diff_text, chunk_size=1500, overlap=200)
                for idx, chunk in enumerate(chunks):
                    documents.append(chunk)
                    metadatas.append({
                        "repo_id": repo_id,
                        "type": "pr_diff",
                        "source": f"PR: {subject}"
                    })
                    ids.append(f"{repo_id}_pr_{sha}_{idx}")
        logger.info("indexed_recent_prs", pr_count=len(pr_commits))

    # 4. Save to ChromaDB
    if not documents:
        logger.warning("no_documents_extracted_nothing_to_index", repo_id=repo_id)
        return

    client = _get_chroma_client()
    collection = _get_collection(client)

    # Clean old entries for namespace to avoid duplicated/stale data.
    # If running in local persistent client mode, lock writes via process-level FileLock
    # to prevent SQLite operational locks if concurrent celery tasks execute.
    lock_file = "./chroma_data/chroma_write.lock"
    os.makedirs("./chroma_data", exist_ok=True)

    # Apply lock if persistent local SQLite is used, otherwise bypass (managed by server)
    lock = FileLock(lock_file) if _is_persistent_local else None

    def _execute_chroma_writes():
        try:
            collection.delete(where={"repo_id": repo_id})
            logger.debug("deleted_existing_conventions", repo_id=repo_id)
        except Exception as exc:
            logger.warning("failed_to_delete_old_conventions", repo_id=repo_id, error=str(exc))

        # Batch add documents (splits into batches of 1000 for Chroma scalability)
        batch_size = 1000
        for offset in range(0, len(documents), batch_size):
            collection.add(
                documents=documents[offset:offset+batch_size],
                metadatas=metadatas[offset:offset+batch_size],
                ids=ids[offset:offset+batch_size]
            )

    try:
        if lock:
            with lock:
                _execute_chroma_writes()
        else:
            _execute_chroma_writes()
        logger.info("indexed_repo_conventions_completed", repo_id=repo_id, total_chunks=len(documents))
    except Exception as exc:
        logger.error("failed_to_add_documents_to_chroma", repo_id=repo_id, error=str(exc))
        raise


# ── Production Cloning & Ingestion Helper ────────────────────────────────────

def get_installation_token(installation_id: int) -> str:
    """Get temporary authentication token for GitHub App Installation (never cached)."""
    settings = get_settings()
    integration = GithubIntegration(settings.github_app_id, settings.github_private_key)
    return integration.get_access_token(installation_id).token


def clone_and_index_repo(repo_full_name: str, installation_id: int | None = None) -> None:
    """
    Clones a repository using a shallow clone (depth=1), indexes its conventions,
    and handles automatic cleanup of local disk resources.
    Guarantees cleanups in all exit cases.
    """
    token = None
    if installation_id:
        try:
            token = get_installation_token(installation_id)
        except Exception as exc:
            logger.warning("failed_to_get_installation_token_falling_back", repo=repo_full_name, error=str(exc))

    if not token:
        token = os.environ.get("GITHUB_TOKEN")

    if token:
        clone_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    else:
        clone_url = f"https://github.com/{repo_full_name}.git"

    # tempfile.TemporaryDirectory guarantees cleanup via its finally block
    with tempfile.TemporaryDirectory() as temp_dir:
        logger.info("cloning_repository_for_rag_indexing", repo=repo_full_name)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, temp_dir],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            # Prevent token credentials leakage in task logs
            logger.error("git_clone_failed_sanitized", repo=repo_full_name)
            raise RuntimeError(f"Git clone failed for repository {repo_full_name} (details sanitized)") from None

        index_repo_conventions(repo_id=repo_full_name, repo_path=temp_dir)


# ── Retrieval Function ────────────────────────────────────────────────────────

def retrieve_conventions(repo_id: str, query: str, k: int = 5) -> str:
    """
    Retrieve top-k most relevant convention chunks from ChromaDB.
    Returns them formatted as a single string.
    """
    client = _get_chroma_client()
    collection = _get_collection(client)

    try:
        results = collection.query(
            query_texts=[query],
            n_results=k,
            where={"repo_id": repo_id}
        )
    except Exception as exc:
        logger.error("chromadb_query_failed", repo_id=repo_id, query=query, error=str(exc))
        return "No repository conventions could be retrieved due to a vector database error."

    if not results or not results.get("documents") or not results["documents"][0]:
        logger.info("no_conventions_retrieved", repo_id=repo_id, query=query)
        return "No relevant repository conventions found."

    formatted_parts = []
    documents = results["documents"][0]
    metadatas = results["metadatas"][0] if results.get("metadatas") else []

    for i, doc in enumerate(documents):
        meta = metadatas[i] if i < len(metadatas) else {}
        source = meta.get("source", "unknown")
        chunk_type = meta.get("type", "unknown")
        formatted_parts.append(
            f"--- CONVENTION CHUNK {i+1} (Source: {source}, Type: {chunk_type}) ---\n{doc}"
        )

    logger.info("conventions_retrieved_successfully", repo_id=repo_id, count=len(formatted_parts))
    return "\n\n".join(formatted_parts)
