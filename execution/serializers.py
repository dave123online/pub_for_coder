"""Serializers Position/Trade en lecture seule pour le dashboard client."""
from decimal import Decimal
from rest_framework import serializers
from .models import Position, Trade
from apps.exchanges.models import Candle


class PositionSerializer(serializers.ModelSerializer):
    # Champs calculés pour le dashboard
    entry_zone_range = serializers.SerializerMethodField()
    remaining_base = serializers.SerializerMethodField()
    current_price = serializers.SerializerMethodField()
    market_value = serializers.SerializerMethodField()
    realized_pnl = serializers.SerializerMethodField()
    unrealized_pnl = serializers.SerializerMethodField()
    total_pnl = serializers.SerializerMethodField()
    pnl_pct = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = Position
        fields = [
            "id", "strategy_config", "mode", "entry_zone", "entry_price",
            "total_amount_base", "total_quote_spent", "buy_count",
            "high_water_mark", "is_sealed", "is_fully_closed",
            "first_buy_at", "created_at", "updated_at",
            # Calculés
            "entry_zone_range", "remaining_base", "current_price",
            "market_value", "realized_pnl", "unrealized_pnl",
            "total_pnl", "pnl_pct", "status",
        ]

    # --- helpers (utilisent le prefetch_related "trades") ---
    def _sold_base(self, obj):
        return sum(t.amount_base for t in obj.trades.all() if t.side == "sell")

    def _sold_quote(self, obj):
        return sum(t.amount_quote for t in obj.trades.all() if t.side == "sell")

    def _current_price(self, obj):
        config = obj.strategy_config
        candle = Candle.objects.filter(
            exchange=config.exchange, symbol=config.symbol, timeframe="1d"
        ).order_by("-open_time").first()
        return candle.close if candle else Decimal("0")

    # --- champs calculés ---
    def get_entry_zone_range(self, obj):
        z = obj.entry_zone
        return {"low": z.price_low, "high": z.price_high} if z else None

    def get_remaining_base(self, obj):
        return obj.total_amount_base - self._sold_base(obj)

    def get_current_price(self, obj):
        return self._current_price(obj)

    def get_market_value(self, obj):
        return self.get_remaining_base(obj) * self._current_price(obj)

    def get_realized_pnl(self, obj):
        """P&L déjà encaissé sur les ventes partielles."""
        if obj.total_amount_base == 0:
            return Decimal("0")
        sold_base = self._sold_base(obj)
        cost_of_sold = (sold_base / obj.total_amount_base) * obj.total_quote_spent
        return self._sold_quote(obj) - cost_of_sold

    def get_unrealized_pnl(self, obj):
        """P&L latent sur ce qu'on détient encore."""
        remaining = self.get_remaining_base(obj)
        return remaining * (self._current_price(obj) - obj.entry_price)

    def get_total_pnl(self, obj):
        return self.get_realized_pnl(obj) + self.get_unrealized_pnl(obj)

    def get_pnl_pct(self, obj):
        if obj.total_quote_spent == 0:
            return 0
        return float(self.get_total_pnl(obj) / obj.total_quote_spent * 100)

    def get_status(self, obj):
        if obj.is_fully_closed:
            return "closed"
        if obj.is_sealed:
            return "sealed"
        return "open"


class TradeSerializer(serializers.ModelSerializer):
    zone_range = serializers.SerializerMethodField()

    class Meta:
        model = Trade
        fields = [
            "id", "strategy_config", "position", "zone", "mode", "side",
            "executed_at", "price", "amount_quote", "amount_base",
            "exchange_order_id", "zone_range",
        ]

    def get_zone_range(self, obj):
        z = obj.zone
        return {"low": z.price_low, "high": z.price_high} if z else None

class BacktestRequestSerializer(serializers.ModelSerializer):
    class Meta:
        from .models import BacktestRequest
        model = BacktestRequest
        fields = ["id", "exchange", "symbol", "capital", "start_date",
                  "discount_pct", "status", "results", "error",
                  "created_at", "completed_at"]
        read_only_fields = ["id", "status", "results", "error",
                            "created_at", "completed_at"]

    def validate(self, attrs):
        from apps.exchanges.models import Candle
        symbol = attrs.get("symbol")
        exchange = attrs.get("exchange", "binance")
        exists = Candle.objects.filter(exchange=exchange, symbol=symbol,
                                       timeframe="1d").exists()
        if not exists:
            raise serializers.ValidationError(
                {"symbol": f"Aucun historique pour {symbol} sur {exchange}."})
        return attrs
