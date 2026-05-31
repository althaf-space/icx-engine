from django.contrib import admin

from blog.models import Category, Comment, Post


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "published")


admin.site.register(Category)
admin.site.register(Comment)
