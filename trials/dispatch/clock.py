import time
from datetime import datetime, timezone


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def iso(self) -> str:
        return datetime.fromtimestamp(self.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
