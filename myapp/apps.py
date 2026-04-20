from django.apps import AppConfig


class MyappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'myapp'

    def ready(self):
        # Wire signal handlers (PrepTask auto-derivation on Menu save)
        from . import signals  # noqa: F401
