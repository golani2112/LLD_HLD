import time

class TokenBucket:
    def __init__(self,capacity,refill_rate):
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

    def allow_request(self):
        self._refill()

        if self.tokens>=1:
            self.tokens-=1
            return True
        return False


class RateLimiter:
    def __init__(self,capacity,refill_rate):
        self.capacity=capacity;
        self.refill_rate=refill_rate
        self.buckets={}

    def allow_requests(self,user_id):
        if user_id not in self.buckets:
            
            self.buckets[user_id] =TokenBucket(
                self.capacity,
                self.refill_rate
            )
        
        return self.buckets[user_id].allow_request()
    
def main():
    limiter=RateLimiter(3,1)
    limiter.allow_requests("user2")
    limiter.allow_requests("user2")
    limiter.allow_requests("user2")
    limiter.allow_requests("user2")
    limiter.allow_requests("user2")


if __name__ == "__main__":
    main()
        