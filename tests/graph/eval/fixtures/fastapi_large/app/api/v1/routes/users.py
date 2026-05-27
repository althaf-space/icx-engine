from fastapi import APIRouter, Depends, HTTPException

from app.api.v1.deps import get_user_service
from app.core.security import current_user_id
from app.schemas.user import UserCreate, UserOut
from app.services.user_service import UserService

router = APIRouter()


@router.post("", response_model=UserOut, status_code=201)
def register_user(payload: UserCreate, service: UserService = Depends(get_user_service)):
    try:
        return service.register(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/me", response_model=UserOut)
def read_me(
    user_id: int = Depends(current_user_id),
    service: UserService = Depends(get_user_service),
):
    user = service.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user
