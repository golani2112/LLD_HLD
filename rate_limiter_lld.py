import time
from collections import deque


def _valid_cost(cost, capacity):
    return isinstance(cost, int) and not isinstance(cost, bool) and 0 < cost <= capacity


class RateLimiterStrategy:
    def allow_requests(self, key, cost=1):
        raise NotImplementedError


class RateLimiter:
    def __init__(self, strategy):
        self.strategy = strategy

    def allow_requests(self, key, cost=1):
        return self.strategy.allow_requests(key, cost)


class SlidingBucket:
    def __init__(self, capacity, window_seconds):
        if capacity <= 0 or window_seconds <= 0:
            raise ValueError("capacity and window_seconds must be positive")
        self.capacity = capacity
        self.window_seconds = window_seconds
        self.request_timestamps=deque()

    def _remove_expired(self,now):
        while self.request_timestamps:
            oldest=self.request_timestamps[0]

            if now-oldest>=self.window_seconds:
                self.request_timestamps.popleft()
            else:
                break

    def allow_request(self,cost=1):
        if not _valid_cost(cost, self.capacity):
            return False

        now = time.monotonic()

        self._remove_expired(now)

        if len(self.request_timestamps) + cost <= self.capacity:
            for _ in range(cost):
                self.request_timestamps.append(now)
            return True

        return False
        

class FixedWindowBucket:
    def __init__(self, capacity, window_seconds):
        if capacity <= 0 or window_seconds <= 0:
            raise ValueError("capacity and window_seconds must be positive")
        self.capacity = capacity
        self.window_seconds = window_seconds
        self.window_start = time.monotonic()
        self.request_count = 0

    def allow_request(self, cost=1):
        if not _valid_cost(cost, self.capacity):
            return False

        now = time.monotonic()
        if now - self.window_start >= self.window_seconds:
            windows_elapsed = int((now - self.window_start) // self.window_seconds)
            self.window_start += windows_elapsed * self.window_seconds
            self.request_count = 0

        if self.request_count + cost <= self.capacity:
            self.request_count += cost
            return True
        return False


class TokenBucket:
    def __init__(self,capacity,refill_rate):
        if capacity <= 0 or refill_rate < 0:
            raise ValueError("capacity must be positive and refill_rate non-negative")
        self.capacity=capacity
        self.tokens=capacity
        self.refill_rate=refill_rate
        self.last_refill_time=time.monotonic()

    def _refill(self):
        now=time.monotonic();
        elapsed= now-self.last_refill_time

        new_tokens=elapsed*self.refill_rate
        self.tokens = min(
            self.capacity,
            self.tokens + new_tokens
        )
        self.last_refill_time=now

    def allow_request(self,cost=1):
        if not _valid_cost(cost, self.capacity):
            return False
        self._refill()

        if self.tokens>=cost:
            self.tokens-=cost
            return True
        return False

class TokenBucketStrategy(RateLimiterStrategy):
    def __init__(self,capacity,refill_rate):
        self.capacity=capacity;
        self.refill_rate=refill_rate
        self.buckets={}

    def allow_requests(self,key,cost=1):
        if key not in self.buckets:
            
            self.buckets[key] =TokenBucket(
                self.capacity,
                self.refill_rate
            )
        
        return self.buckets[key].allow_request(cost)
    
class FixedWindowStrategy(RateLimiterStrategy):
    def __init__(self, capacity, window_seconds):
        self.capacity = capacity
        self.window_seconds = window_seconds
        self.buckets = {}

    def allow_requests(self,key,cost=1):
        if key not in self.buckets:
            self.buckets[key] = FixedWindowBucket(
                self.capacity,
                self.window_seconds,
            )
        return self.buckets[key].allow_request(cost)


class SlidingWindowStrategy(RateLimiterStrategy):
    def __init__(self, capacity, window_seconds):
        self.capacity = capacity
        self.window_seconds = window_seconds
        self.buckets = {}

    def allow_requests(self, key, cost=1):
        if key not in self.buckets:
            self.buckets[key] = SlidingBucket(
                self.capacity,
                self.window_seconds,
            )
        return self.buckets[key].allow_request(cost)

           
def main():
    limiter=RateLimiter(TokenBucketStrategy(capacity=3,refill_rate=1))
    limiter.allow_requests("user2",cost=1)
    limiter.allow_requests("user2")
    limiter.allow_requests("user2")
    limiter.allow_requests("user2")
    limiter.allow_requests("user2")


if __name__ == "__main__":
    main()
        