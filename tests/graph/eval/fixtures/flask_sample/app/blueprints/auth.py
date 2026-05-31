from flask import Blueprint, jsonify, request

from app.services import fetch_user

auth_bp = Blueprint("auth", __name__)


@auth_bp.before_request
def log_request():
    print(f"[auth] {request.method} {request.path}")


@auth_bp.post("/login")
def login():
    user = fetch_user(int(request.json.get("user_id", 0)))
    if user is None:
        return jsonify({"error": "no user"}), 404
    return jsonify({"token": str(user.id)})
