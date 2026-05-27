from sqlalchemy.orm import Session

from app.models import User
from app.schemas import UserCreate


class UserService:
    def __init__(self, db: Session):
        self.db = db

    def list_users(self) -> list[User]:
        return self.db.query(User).all()

    def get_user(self, user_id: int) -> User | None:
        return self.db.query(User).filter(User.id == user_id).first()

    def create_user(self, payload: UserCreate) -> User:
        user = User(email=payload.email, name=payload.name)
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user
