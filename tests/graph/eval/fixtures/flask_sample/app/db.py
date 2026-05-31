from flask import Flask


def init_db(app: Flask) -> None:
    app.config.setdefault("DB_URL", "sqlite:///fixture.db")


def get_connection():
    return {"placeholder": True}
