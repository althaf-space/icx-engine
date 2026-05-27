from typing import Protocol

class Notifier(Protocol):
    def send(self, message: str) -> bool: ...

class Storage(Protocol):
    def save(self, key: str, value: str) -> None: ...
