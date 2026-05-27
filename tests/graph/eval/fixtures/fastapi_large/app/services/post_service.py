from app.db.models.post import Post
from app.db.repositories.post_repo import PostRepo
from app.schemas.post import PostCreate


class PostService:
    def __init__(self, repo: PostRepo):
        self.repo = repo

    def list_for_author(self, author_id: int) -> list[Post]:
        return self.repo.list_for_author(author_id)

    def publish(self, author_id: int, payload: PostCreate) -> Post:
        return self.repo.create(
            author_id=author_id,
            title=payload.title,
            body=payload.body,
        )
