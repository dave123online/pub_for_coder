from celery import shared_task

from .models import StrategyConfig


@shared_task
def scan_all_active_zones():
    """Exécutée toutes les 4h par Celery Beat : traite chaque config active en mode test/live."""
    from apps.execution.engine import run_test_or_live  # import tardif pour éviter les imports circulaires

    for config in StrategyConfig.objects.filter(is_active=True, mode__in=["test", "live"]):
        run_test_or_live(config)


@shared_task
def run_walkforward_task(config_id):
    from apps.execution.engine import run_walkforward
    from .models import StrategyConfig as SC

    config = SC.objects.get(id=config_id)
    run_walkforward(config)
