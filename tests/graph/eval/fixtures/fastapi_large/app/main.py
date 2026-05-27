from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.middleware import RequestIdMiddleware

app = FastAPI(title="fastapi_large eval fixture")
app.add_middleware(RequestIdMiddleware)
app.include_router(api_router, prefix="/v1")
