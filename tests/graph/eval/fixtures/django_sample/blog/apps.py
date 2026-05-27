from django.apps import AppConfig


class BlogConfig(AppConfig):
    name = "blog"

    def ready(self):
        from blog import signals  # noqa: F401  registers handlers
