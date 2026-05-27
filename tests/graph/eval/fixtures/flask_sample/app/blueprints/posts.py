from flask import Blueprint, jsonify, request

from app.services import list_posts_for

posts_bp = Blueprint("posts", __name__)


@posts_bp.get("/")
def list_my_posts():
    author_id = int(request.args.get("user", 0))
    posts = list_posts_for(author_id)
    return jsonify([{"id": p.id, "title": p.title} for p in posts])
