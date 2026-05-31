from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.repositories.post_repo import PostRepo
from app.db.repositories.user_repo import UserRepo
from app.db.session import get_db
from app.services.post_service import PostService
from app.services.user_service import UserService


def get_user_repo(db: Session = Depends(get_db)) -> UserRepo:
    return UserRepo(db)


def get_post_repo(db: Session = Depends(get_db)) -> PostRepo:
    return PostRepo(db)


def get_user_service(repo: UserRepo = Depends(get_user_repo)) -> UserService:
    return UserService(repo)


def get_post_service(repo: PostRepo = Depends(get_post_repo)) -> PostService:
    return PostService(repo)
