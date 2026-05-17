import asyncio
from time import time


class RateLimiter:
    def __init__(self, max_requests: int = 2, time_window: int = 1):
        """
        Rate limiter using token bucket algorithm.
        max_requests: maximum requests per time_window
        time_window: time window in seconds (default: 1 second)
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.tokens = max_requests
        self.last_update = time()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire a token. Wait if necessary."""
        async with self._lock:
            now = time()
            elapsed = now - self.last_update
            self.last_update = now

            # Refill tokens
            self.tokens = min(
                self.max_requests,
                self.tokens + elapsed * (self.max_requests / self.time_window)
            )

            if self.tokens < 1:
                # Calculate how long to wait
                wait_time = (1 - self.tokens) * (self.time_window / self.max_requests)
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= 1
