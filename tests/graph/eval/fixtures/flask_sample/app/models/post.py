from dataclasses import dataclass


@dataclass
class Post:
    id: int
    author_id: int
    title: str
    body: str
