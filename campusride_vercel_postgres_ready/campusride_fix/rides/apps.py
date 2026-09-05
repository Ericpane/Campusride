from django.apps import AppConfig


class RidesConfig(AppConfig):
    name = 'rides'

    def ready(self):
        # Registers the ML model health check (rides.checks.check_ml_models)
        # so `manage.py check` / runserver / test fail loudly if a model
        # file fails to load or its feature count no longer matches what
        # ml_service.py sends it.
        from . import checks  # noqa: F401
