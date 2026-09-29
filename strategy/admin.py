from django.contrib import admin
from django.apps import apps
from .models import Zone, StrategyConfig


@admin.register(StrategyConfig)
class StrategyConfigAdmin(admin.ModelAdmin):
    list_display = ("user", "symbol", "exchange", "mode", "balance_quote",
                    "buy_streak_schedule", "use_trend_filter", "discount_pct", "is_active")
    list_filter = ("exchange", "mode", "is_active", "use_trend_filter")
    search_fields = ("symbol", "user__username")
    fieldsets = (
        (None, {"fields": ("user", "exchange", "exchange_connection", "symbol", "mode", "is_active")}),
        ("Capital & streak", {"fields": ("balance_quote", "buy_streak_schedule")}),
        ("Filtres", {"fields": ("use_trend_filter", "trend_ema_period", "discount_pct",
                                "fast_break_candles", "leave_atr_multiplier")}),
        ("Anciens champs (remplacés par le streak)", {
            "classes": ("collapse",),
            "fields": ("percent_per_buy", "max_buys_per_zone"),
        }),
        ("Etat", {"fields": ("target_zone",)}),
    )


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ("symbol", "exchange", "timeframe", "price_low", "price_high",
                    "touch_count", "label", "is_validated", "is_manual",
                    "percent_per_buy_override", "max_buys_override", "is_active")
    list_filter = ("exchange", "symbol", "timeframe", "is_validated", "is_manual", "is_active")
    search_fields = ("symbol", "label")
    ordering = ("symbol", "price_low")
    list_editable = ("label", "is_validated", "percent_per_buy_override", "max_buys_override")
    list_per_page = 50
    fieldsets = (
        ("Identification", {"fields": ("exchange", "symbol", "timeframe", "label")}),
        ("Prix", {"fields": ("price_low", "price_high")}),
        ("Trading", {"fields": ("is_validated", "percent_per_buy_override", "max_buys_override")}),
        ("Statut", {"fields": ("is_active", "is_manual", "touch_count", "score")}),
        ("Dates", {"fields": ("first_seen", "last_touched"), "classes": ("collapse",)}),
    )

    def save_model(self, request, obj, form, change):
        """Toute zone CRÉÉE depuis l'admin est marquée manuelle (préservée des recalculs)."""
        if not change:
            obj.is_manual = True
        super().save_model(request, obj, form, change)


for model in apps.get_app_config("strategy").get_models():
    if model in (Zone, StrategyConfig):
        continue
    try:
        admin.site.register(model)
    except admin.sites.AlreadyRegistered:
        pass
    