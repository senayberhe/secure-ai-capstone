"""Redis notifications and atomic fixed-window limits; polling is the durable fallback."""
import threading
import time

RATE_SCRIPT = """
local v = redis.call('INCR', KEYS[1])
if v == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return v
"""


class MemoryRateLimiter:
    def __init__(self):
        self.state = {}
        self.lock = threading.Lock()

    def allow(self, identity: str, limit=30, window=60) -> bool:
        with self.lock:
            now = time.monotonic()
            count, expiry = self.state.get(identity, (0, now + window))
            if expiry <= now:
                count, expiry = 0, now + window
            count += 1
            self.state[identity] = (count, expiry)
            return count <= limit


class RedisNotifier:
    def __init__(self, url):
        import redis
        self.client = redis.Redis.from_url(url, decode_responses=True)

    def ping(self):
        return self.client.ping()

    def notify(self, job_id):
        self.client.lpush("capstone:wake", job_id)
        self.client.ltrim("capstone:wake", 0, 9999)

    def wait(self, seconds=2):
        return self.client.brpop("capstone:wake", timeout=seconds)

    def allow(self, identity, limit=30, window=60):
        # Identity should be a stable opaque hash, not a raw email or token.
        key = f"capstone:lim:{identity}"
        return int(self.client.eval(RATE_SCRIPT, 1, key, window)) <= limit
