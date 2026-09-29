from django.conf import settings
from django.db import models
from decimal import Decimal

from apps.exchanges.models import ExchangeConnection


class StrategyConfig(models.Model):
    """Configuration d'une stratégie DCA-sur-zones pour un utilisateur, un exchange et une paire donnés."""

    MODE_CHOICES = [
        ("test", "Test (paper trading)"),
        ("live", "Live"),
        ("walkforward", "Walkforward (backtest historique complet)"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="strategy_configs")
    exchange_connection = models.ForeignKey(ExchangeConnection, on_delete=models.CASCADE, null=True, blank=True,
                                             help_text="Requis en mode live, optionnel en test/walkforward")
    exchange = models.CharField(max_length=20, choices=ExchangeConnection.EXCHANGE_CHOICES)
    symbol = models.CharField(max_length=20)  # ex: BTC/USDT
    mode = models.CharField(max_length=15, choices=MODE_CHOICES)

    balance_quote = models.DecimalField(max_digits=24, decimal_places=10, help_text="Solde de départ en devise de cotation")
    percent_per_buy = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("1.00"),
        help_text="% du solde alloué par achat (défaut, surchargeable par zone)")
    max_buys_per_zone = models.PositiveIntegerField(
        default=3,
        help_text="Nb max d'achats sur une même zone avant reset (défaut, surchargeable par zone)")
    leave_atr_multiplier = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("2.00"),
        help_text="Multiplicateur d'ATR pour détecter l'éloignement de zone (reset)")

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Etat de l'algo : uniquement la cible d'achat courante. Les positions ouvertes
    # (avec leur propre echelle de TP) sont trackees dans execution.models.Position, pas ici,
    # car plusieurs positions distinctes peuvent etre ouvertes simultanement (supports casses).
    target_zone = models.ForeignKey(
        "Zone", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="Support immediatement en dessous du prix, cible d'achat actuelle"
    )
    buy_streak_schedule = models.JSONField(
        default=list, blank=True,
        help_text="% par achat dans la série, ex [2,2,1] = 5%/zone/streak. Vide = [2,2,1]")
    use_trend_filter = models.BooleanField(default=True)
    trend_ema_period = models.PositiveIntegerField(default=200)
    discount_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("75.00"),
        help_text="Profondeur de correction minimale (fib %) pour acheter. "
                  "75 = discount profond (RR 3), 50 = équilibre. Ajustable par actif.")
    fast_break_candles = models.PositiveIntegerField(
        default=3, help_text="Cassure de la zone ≤ N bougies après le 1er achat = zone invalidée")

    def __str__(self):
        return f"{self.user} - {self.symbol} [{self.mode}]"


class Zone(models.Model):
    """Zone de support/résistance détectée : cluster de pivots proches sur un timeframe donné."""

    TIMEFRAME_CHOICES = [
        ("4h", "H4"),
        ("1d", "Daily"),
        ("1w", "Weekly"),
    ]

    exchange = models.CharField(max_length=20)
    symbol = models.CharField(max_length=20)
    timeframe = models.CharField(max_length=5, choices=TIMEFRAME_CHOICES)

    price_low = models.DecimalField(max_digits=24, decimal_places=10)
    price_high = models.DecimalField(max_digits=24, decimal_places=10)
    touch_count = models.PositiveIntegerField(default=1)
    score = models.FloatField(default=0.0, help_text="Score de force de la zone (touches, ancienneté, réactions)")

    first_seen = models.DateTimeField()
    last_touched = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    is_manual = models.BooleanField(
        default=False,
        help_text="Zone créée manuellement via l'admin (préservée lors des recalculs auto)"
    )

    # --- Champs éditables humainement (admin) ---
    label = models.CharField(max_length=100, blank=True, default="",
                             help_text="Label humain pour identifier la zone")
    is_validated = models.BooleanField(default=False,
                                       help_text="Zone confirmée humainement")
    percent_per_buy_override = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="% d'achat propre à cette zone (remplace le défaut config si défini)")
    max_buys_override = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Max achats propre à cette zone (remplace le défaut config si défini)")

    class Meta:
        indexes = [models.Index(fields=["exchange", "symbol", "timeframe", "is_active"])]

    def __str__(self):
        return f"{self.symbol} [{self.price_low}-{self.price_high}] ({self.timeframe}, {self.touch_count} touches)"


class ZoneEvent(models.Model):
    """Historique des moments où le prix a réagi sur une zone (utilisé aussi bien en live qu'en walkforward)."""

    zone = models.ForeignKey(Zone, on_delete=models.CASCADE, related_name="events")
    occurred_at = models.DateTimeField()
    price_at_event = models.DecimalField(max_digits=24, decimal_places=10)

    class Meta:
        ordering = ["occurred_at"]
