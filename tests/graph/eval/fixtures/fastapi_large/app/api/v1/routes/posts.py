from fastapi import APIRouter, Depends

from app.api.v1.deps import get_post_service
from app.core.security import current_user_id
from app.schemas.post import PostCreate, PostOut
from app.services.post_service import PostService

router = APIRouter()


@router.get("", response_model=list[PostOut])
def list_my_posts(
    user_id: int = Depends(current_user_id),
    service: PostService = Depends(get_post_service),
):
    return service.list_for_author(user_id)


@router.post("", response_model=PostOut, status_code=201)
def publish_post(
    payload: PostCreate,
    user_id: int = Depends(current_user_id),
    service: PostService = Depends(get_post_service),
):
    return service.publish(user_id, payload)
