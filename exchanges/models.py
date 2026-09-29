from django.conf import settings
from django.db import models
from .fields import EncryptedCharField


class ExchangeConnection(models.Model):
    """Connexion API d'un utilisateur à un exchange (Binance, Bybit, OKX)."""

    EXCHANGE_CHOICES = [
        ("binance", "Binance"),
        ("bybit", "Bybit"),
        ("okx", "OKX"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="exchange_connections")
    exchange = models.CharField(max_length=20, choices=EXCHANGE_CHOICES)
    label = models.CharField(max_length=50, blank=True, help_text="Nom libre pour distinguer plusieurs connexions au même exchange")

    # Chiffrés au repos via django-cryptography (Fernet, clé = settings.FIELD_ENCRYPTION_KEY)
    api_key = EncryptedCharField(max_length=500)
    api_secret = EncryptedCharField(max_length=500)
    api_passphrase = EncryptedCharField(max_length=500, blank=True, null=True)  # requis par OKX

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "exchange", "label")

    def __str__(self):
        return f"{self.user} - {self.exchange} ({self.label or 'default'})"


class Candle(models.Model):
    """Cache local des bougies OHLCV par exchange/pair/timeframe, mis à jour de manière incrémentale."""

    TIMEFRAME_CHOICES = [
        ("4h", "H4"),
        ("1d", "Daily"),
        ("1w", "Weekly"),
    ]

    exchange = models.CharField(max_length=20, choices=ExchangeConnection.EXCHANGE_CHOICES)
    symbol = models.CharField(max_length=20)  # ex: BTC/USDT
    timeframe = models.CharField(max_length=5, choices=TIMEFRAME_CHOICES)
    open_time = models.DateTimeField()
    open = models.DecimalField(max_digits=24, decimal_places=10)
    high = models.DecimalField(max_digits=24, decimal_places=10)
    low = models.DecimalField(max_digits=24, decimal_places=10)
    close = models.DecimalField(max_digits=24, decimal_places=10)
    volume = models.DecimalField(max_digits=30, decimal_places=10)

    class Meta:
        unique_together = ("exchange", "symbol", "timeframe", "open_time")
        indexes = [
            models.Index(fields=["exchange", "symbol", "timeframe", "open_time"]),
        ]
        ordering = ["open_time"]

    def __str__(self):
        return f"{self.exchange}:{self.symbol}:{self.timeframe} @ {self.open_time}"
