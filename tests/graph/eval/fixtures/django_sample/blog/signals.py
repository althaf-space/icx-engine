from django.db.models.signals import post_save
from django.dispatch import receiver

from blog.models import Post
from blog.services import post_word_count


@receiver(post_save, sender=Post)
def log_post_save(sender, instance: Post, created: bool, **kwargs) -> None:
    word_count = post_word_count(instance)
    print(f"[blog] post {instance.pk} saved ({word_count} words, created={created})")
