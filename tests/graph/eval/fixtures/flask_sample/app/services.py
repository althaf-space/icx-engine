from app.db import get_connection
from app.models.post import Post
from app.models.user import User


def fetch_user(user_id: int) -> User | None:
    _ = get_connection()
    return User(id=user_id, email="x@y", name="fixture")


def list_posts_for(author_id: int) -> list[Post]:
    _ = get_connection()
    return [Post(id=1, author_id=author_id, title="hi", body="world")]
