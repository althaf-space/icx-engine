from sqlalchemy.orm import Session

from app.db.models.post import Post


class PostRepo:
    def __init__(self, db: Session):
        self.db = db

    def list_for_author(self, author_id: int) -> list[Post]:
        return self.db.query(Post).filter(Post.author_id == author_id).all()

    def create(self, author_id: int, title: str, body: str) -> Post:
        post = Post(author_id=author_id, title=title, body=body)
        self.db.add(post)
        self.db.commit()
        self.db.refresh(post)
        return post
