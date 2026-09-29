from django.conf import settings
from django.db import models
from apps.strategy.models import StrategyConfig, Zone
from decimal import Decimal


class Position(models.Model):
    """Position spot ouverte sur un support donne, avec sa propre echelle de TP
    (calculee une fois a la creation, a partir des zones connues a ce moment-la).

    resistance_ladder : liste ordonnee [{"zone_id": int, "percentage": float, "closed": bool}, ...]
    Les pourcentages s'appliquent a total_amount_base (le total accumule tant que la position
    n'a pas encore ete touchee par un premier TP partiel - au-dela, plus de fusion possible).
    """

    strategy_config = models.ForeignKey(StrategyConfig, on_delete=models.CASCADE, related_name="positions")
    mode = models.CharField(max_length=15, choices=StrategyConfig.MODE_CHOICES)
    entry_zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="+")

    entry_price = models.DecimalField(max_digits=24, decimal_places=10, help_text="Prix moyen pondere d'entree")
    total_amount_base = models.DecimalField(max_digits=24, decimal_places=10, default=0)
    total_quote_spent = models.DecimalField(max_digits=24, decimal_places=10, default=0)

    resistance_ladder = models.JSONField(default=list)
    is_fully_closed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    buy_count = models.PositiveIntegerField(default=0,
        help_text="Nb d'achats accumulés sur cette position")
    high_water_mark = models.DecimalField(max_digits=24, decimal_places=10, default=0,
        help_text="Prix le plus haut vu depuis l'ouverture (détection d'éloignement)")
    is_sealed = models.BooleanField(default=False,
        help_text="Accumulation scellée (prix parti au-delà du seuil ATR) — plus de fusion")
    first_buy_at = models.DateTimeField(null=True, blank=True)

    @property
    def has_been_touched(self):
        """True des qu'au moins un palier de TP a ete declenche -> plus aucune fusion possible."""
        return any(step["closed"] for step in self.resistance_ladder)

    def __str__(self):
        return f"Position {self.strategy_config.symbol} entry={self.entry_price} ({'closed' if self.is_fully_closed else 'open'})"


class Trade(models.Model):
    MODE_CHOICES = StrategyConfig.MODE_CHOICES
    SIDE_CHOICES = [("buy", "Buy"), ("sell", "Sell (TP partiel ou total)")]

    strategy_config = models.ForeignKey(StrategyConfig, on_delete=models.CASCADE, related_name="trades")
    position = models.ForeignKey(Position, on_delete=models.CASCADE, related_name="trades", null=True, blank=True)
    zone = models.ForeignKey(Zone, on_delete=models.SET_NULL, null=True)
    mode = models.CharField(max_length=15, choices=MODE_CHOICES)
    side = models.CharField(max_length=4, choices=SIDE_CHOICES, default="buy")

    executed_at = models.DateTimeField()
    price = models.DecimalField(max_digits=24, decimal_places=10)
    amount_quote = models.DecimalField(max_digits=24, decimal_places=10)
    amount_base = models.DecimalField(max_digits=24, decimal_places=10)

    exchange_order_id = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        ordering = ["-executed_at"]

    def __str__(self):
        return f"{self.strategy_config.symbol} {self.side} @ {self.price} ({self.mode})"


class BacktestRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "En attente"),
        ("running", "En cours"),
        ("completed", "Terminé"),
        ("failed", "Échoué"),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="backtests")
    exchange = models.CharField(max_length=32, default="binance")
    symbol = models.CharField(max_length=20)
    capital = models.DecimalField(max_digits=24, decimal_places=10)
    start_date = models.DateField(null=True, blank=True)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("75"))
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="pending")
    celery_task_id = models.CharField(max_length=255, blank=True)
    results = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Backtest {self.symbol} {self.capital} ({self.status})"
