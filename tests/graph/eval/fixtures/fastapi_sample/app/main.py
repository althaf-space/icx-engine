from fastapi import Depends, FastAPI, HTTPException

from app.db import get_db
from app.schemas import UserCreate, UserOut
from app.services.user_service import UserService

app = FastAPI()


@app.get("/users", response_model=list[UserOut])
def list_users(db=Depends(get_db)):
    return UserService(db).list_users()


@app.get("/users/{user_id}", response_model=UserOut)
def read_user(user_id: int, db=Depends(get_db)):
    user = UserService(db).get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, db=Depends(get_db)):
    return UserService(db).create_user(payload)
