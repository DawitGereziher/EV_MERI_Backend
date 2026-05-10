from django.apps import AppConfig


class ChargingStationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "charging_stations"

    def ready(self):
        import charging_stations.signals  # noqa: F401 — loads all signal handlers
