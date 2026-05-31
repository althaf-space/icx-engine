from app.core.security import hash_password, verify_password
from app.db.repositories.user_repo import UserRepo
from app.db.models.user import User
from app.schemas.user import UserCreate


class UserService:
    def __init__(self, repo: UserRepo):
        self.repo = repo

    def register(self, payload: UserCreate) -> User:
        existing = self.repo.find_by_email(payload.email)
        if existing:
            raise ValueError("email already in use")
        return self.repo.create(
            email=payload.email,
            name=payload.name,
            password_hash=hash_password(payload.password),
        )

    def authenticate(self, email: str, raw_password: str) -> User | None:
        user = self.repo.find_by_email(email)
        if user is None:
            return None
        if not verify_password(raw_password, user.password_hash):
            return None
        return user

    def get(self, user_id: int) -> User | None:
        return self.repo.get(user_id)
