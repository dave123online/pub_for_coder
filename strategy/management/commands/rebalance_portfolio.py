from decimal import Decimal
from django.core.management.base import BaseCommand
from apps.strategy.models import StrategyConfig


class Command(BaseCommand):
    help = "Répartit un capital total à parts égales sur les configs actives d'un utilisateur"

    def add_arguments(self, parser):
        parser.add_argument("user_id", type=int)
        parser.add_argument("total_quote", type=str)

    def handle(self, *args, **opts):
        configs = StrategyConfig.objects.filter(user_id=opts["user_id"], is_active=True)
        if not configs:
            self.stderr.write("Aucune config active pour cet utilisateur")
            return
        share = Decimal(opts["total_quote"]) / Decimal(len(configs))
        for c in configs:
            c.balance_quote = share
            c.save(update_fields=["balance_quote"])
            self.stdout.write(f"  {c.symbol}: {share}")
        self.stdout.write(self.style.SUCCESS(f"Réparti sur {len(configs)} actifs"))
