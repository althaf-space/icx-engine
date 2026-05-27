from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.v1.deps import get_user_service
from app.services.user_service import UserService

router = APIRouter()


@router.post("/token")
def issue_token(
    form: OAuth2PasswordRequestForm = Depends(),
    service: UserService = Depends(get_user_service),
):
    user = service.authenticate(form.username, form.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad credentials")
    return {"access_token": str(user.id), "token_type": "bearer"}
