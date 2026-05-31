from flask import Flask

from app.blueprints.auth import auth_bp
from app.blueprints.posts import posts_bp
from app.db import init_db


def create_app() -> Flask:
    app = Flask(__name__)
    init_db(app)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(posts_bp, url_prefix="/posts")
    return app
