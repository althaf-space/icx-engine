from django.urls import path
from myapp.api import views

urlpatterns = [
    path("users/", views.list_users),
    path("users/<int:pk>/", views.get_user),
]
