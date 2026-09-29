"""Serializers pour les configurations de stratégie."""
from rest_framework import serializers
from django.db.models import Min, Max
from .models import StrategyConfig
from apps.exchanges.models import ExchangeConnection, Candle


class StrategyConfigSerializer(serializers.ModelSerializer):
    # Lecture seule : infos calculées
    symbol_base = serializers.SerializerMethodField()
    is_running = serializers.SerializerMethodField()
    position_count = serializers.SerializerMethodField()
    
    # Validation de la connexion
    exchange_connection = serializers.PrimaryKeyRelatedField(
        queryset=ExchangeConnection.objects.all()
    )

    class Meta:
        model = StrategyConfig
        fields = [
            "id", "exchange", "symbol", "mode", "balance_quote",
            "discount_pct", "is_active", "exchange_connection",
            "symbol_base", "is_running", "position_count",
        ]
        read_only_fields = ["id", "symbol_base", "is_running", "position_count"]

    def get_symbol_base(self, obj):
        return obj.symbol.split("/")[0] if "/" in obj.symbol else obj.symbol

    def get_is_running(self, obj):
        return obj.is_active and obj.mode == "live"

    def get_position_count(self, obj):
        return obj.positions.count()

    def validate_exchange_connection(self, value):
        """La connexion doit appartenir à l'utilisateur."""
        if value.user != self.context["request"].user:
            raise serializers.ValidationError(
                "Cette connexion ne vous appartient pas.")
        return value

    def validate(self, attrs):
        """Valide que l'actif a assez d'historique pour le backtest."""
        symbol = attrs.get("symbol")
        exchange = attrs.get("exchange", "binance")
        
        # Vérifie qu'on a au moins 1 an d'historique
        candle_stats = Candle.objects.filter(
            exchange=exchange, symbol=symbol, timeframe="1d"
        ).aggregate(
            first=Min("open_time"),
            last=Max("open_time"),
        )
        
        if not candle_stats["first"] or not candle_stats["last"]:
            raise serializers.ValidationError({
                "symbol": f"Aucun historique de bougies trouvé pour {symbol} sur {exchange}."
            })
        
        history_days = (candle_stats["last"] - candle_stats["first"]).days
        if history_days < 365:
            raise serializers.ValidationError({
                "symbol": f"Historique insuffisant ({history_days} jours). Minimum requis : 365 jours."
            })
        
        return attrs
