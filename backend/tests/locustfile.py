import os
import json
import hmac
import hashlib
from locust import HttpUser, task, between

SAMPLE_PAYLOAD = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "head": {"sha": "abc1234567890"},
        "title": "Add feature X",
        "user": {"login": "testuser"},
        "changed_files": 3,
        "additions": 100,
        "deletions": 20,
    },
    "repository": {"full_name": "testowner/testrepo", "id": 999},
    "installation": {"id": 777},
}

class WebhookUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task
    def post_webhook(self):
        body = json.dumps(SAMPLE_PAYLOAD).encode()
        # Retrieve webhook secret from settings/env
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "test-secret")
        
        # Calculate webhook signature
        sig = hmac.new(
            key=secret.encode(),
            msg=body,
            digestmod=hashlib.sha256,
        ).hexdigest()
        signature = f"sha256={sig}"

        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "locust-delivery-uuid",
            "X-Hub-Signature-256": signature,
        }

        # POST payload to the FastAPI webhook endpoint
        self.client.post("/webhooks/github", data=body, headers=headers)
