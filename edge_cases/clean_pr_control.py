# clean_pr_control.py — A negative control PR with zero security or design flaws.
# Implements a standard token bucket rate limiter with proper synchronization.

import time
from threading import Lock


class TokenBucketLimiter:
    def __init__(self, capacity: int, fill_rate: float):
        if capacity <= 0 or fill_rate <= 0:
            raise ValueError("Limiter capacity and fill rate must be positive values.")
        self.capacity = capacity
        self.fill_rate = fill_rate
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.lock = Lock()

    def consume(self, amount: int = 1) -> bool:
        if amount <= 0:
            raise ValueError("Consumption amount must be at least 1.")
        
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.last_refill = now
            
            # Refill tokens
            self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
            
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False
