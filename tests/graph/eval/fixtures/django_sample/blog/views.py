from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import DetailView, ListView

from blog.forms import PostForm
from blog.models import Post
from blog.services import publish_post


class PostListView(ListView):
    model = Post
    template_name = "blog/post_list.html"


class PostDetailView(DetailView):
    model = Post
    template_name = "blog/post_detail.html"


def create_post(request):
    if request.method == "POST":
        form = PostForm(request.POST)
        if form.is_valid():
            post = form.save()
            return redirect("blog:post-detail", pk=post.pk)
    else:
        form = PostForm()
    return render(request, "blog/post_form.html", {"form": form})


def publish_post_view(request, pk):
    post = get_object_or_404(Post, pk=pk)
    publish_post(post)
    return redirect("blog:post-detail", pk=post.pk)


def health_check(request):
    return JsonResponse({"status": "ok"})
