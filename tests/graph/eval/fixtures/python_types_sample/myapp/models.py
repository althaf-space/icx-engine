from dataclasses import dataclass
from myapp.protocols import Notifier

@dataclass
class Order:
    id: int
    item: str
    notifier: Notifier
