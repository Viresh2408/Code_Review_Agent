"""
Celery application factory.
"""

from __future__ import annotations

from celery import Celery

from app.config import get_settings
from app.observability.logging_config import setup_logging

# Initialize unified structured logging config for celery worker
setup_logging()

settings = get_settings()

celery_app = Celery(
    "codereview",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.review_job"],
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Reliability: retry failed tasks up to 3× with exponential back-off
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Routing: all PR review jobs go to the dedicated queue
    task_default_queue="pr_review",
    task_queues={
        "pr_review": {
            "exchange": "pr_review",
            "routing_key": "pr_review",
        }
    },
    # Timezone
    timezone="UTC",
    enable_utc=True,
)
