def list_users(request):
    return {"users": []}


def get_user(request, pk: int):
    return {"id": pk}
