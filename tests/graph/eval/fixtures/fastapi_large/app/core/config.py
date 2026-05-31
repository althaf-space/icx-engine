from pydantic import BaseModel


class Settings(BaseModel):
    database_url: str = "sqlite:///./eval.db"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
