import asyncio
import datetime
import random
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

# Set up paths so we can import from app
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.models import Repo, PullRequest, Review, Finding, DebtScore

settings = get_settings()
engine = create_async_engine(settings.database_url)
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def seed_data():
    if "sqlite" in settings.database_url:
        from app.db.base import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("Created SQLite tables.")

    async with AsyncSessionLocal() as session:
        # 1. Create or get test repository
        repo_stmt = select(Repo).where(Repo.owner == "owner", Repo.name == "repo")
        res = await session.execute(repo_stmt)
        repo = res.scalar_one_or_none()
        if not repo:
            repo = Repo(
                github_repo_id=12345678,
                owner="owner",
                name="repo",
                is_active=True,
            )
            session.add(repo)
            await session.flush()
            print("Created Repo: owner/repo")
        else:
            print("Found existing Repo: owner/repo")

        # 2. Seed 10 historical PRs and Reviews
        files = [
            "src/orders/service.py",
            "src/payments/handler.py",
            "src/auth/login.py",
            "src/utils/helpers.py",
        ]
        
        # Cumulative score starting points
        current_scores = {f: 5.0 for f in files}
        
        base_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
        
        for pr_idx in range(1, 11):
            pr_time = base_time + datetime.timedelta(days=pr_idx * 3)
            commit_sha = f"sha{random.randint(100000, 999999)}"
            
            # Create Pull Request
            pr = PullRequest(
                repo_id=repo.id,
                pr_number=pr_idx,
                commit_sha=commit_sha,
                status="completed",
                created_at=pr_time,
                completed_at=pr_time + datetime.timedelta(minutes=15),
            )
            session.add(pr)
            await session.flush()
            
            # Create Review
            findings_count = random.randint(1, 4)
            blockers = random.choice([0, 0, 1])
            warnings = findings_count - blockers
            nits = random.randint(1, 3)
            
            review = Review(
                pull_request_id=pr.id,
                total_findings=findings_count + nits,
                blocker_count=blockers,
                warning_count=warnings,
                nit_count=nits,
                model_cost_usd=0.04,
                duration_ms=2500,
                created_at=pr_time + datetime.timedelta(minutes=10),
            )
            session.add(review)
            await session.flush()
            
            # Seed findings
            for f_idx in range(findings_count + nits):
                sev = "nit"
                if f_idx < blockers:
                    sev = "blocker"
                elif f_idx < blockers + warnings:
                    sev = "warning"
                    
                finding = Finding(
                    review_id=review.id,
                    agent=random.choice(["security_agent", "architecture_agent", "test_coverage_agent"]),
                    file_path=random.choice(files),
                    line_number=random.randint(10, 200),
                    severity=sev,
                    category=random.choice(["security", "architecture", "test-coverage"]),
                    message=f"Mock issue {f_idx + 1} of severity {sev} detected in codebase.",
                    confidence=0.85,
                    escalated_to_claude=False,
                    suggested_fix="Use parameterized queries or standard helper utility functions.",
                    created_at=pr_time + datetime.timedelta(minutes=10),
                )
                session.add(finding)
            
            # Seed Debt Scores (Progression)
            for f in files:
                # Random change delta
                delta = random.uniform(-1.5, 2.5)
                # Keep score positive
                current_scores[f] = max(0.5, current_scores[f] + delta)
                
                score_record = DebtScore(
                    time=pr_time,
                    repo_id=repo.id,
                    file_path=f,
                    score=current_scores[f],
                    delta=delta,
                    pr_number=pr_idx,
                )
                session.add(score_record)
                
            print(f"Seeded PR #{pr_idx} and Review ID #{review.id}")
            
        await session.commit()
        print("Success! Successfully seeded 10 processed PRs, reviews, and historical debt score trends.")

if __name__ == "__main__":
    asyncio.run(seed_data())
