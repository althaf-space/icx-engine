from pydantic import BaseModel


class PostBase(BaseModel):
    title: str
    body: str


class PostCreate(PostBase):
    pass


class PostOut(PostBase):
    id: int
    author_id: int

    class Config:
        from_attributes = True
