"""Serializers pour les connexions exchange."""
from rest_framework import serializers
from .models import ExchangeConnection


class ExchangeConnectionSerializer(serializers.ModelSerializer):
    # Les secrets sont write-only : JAMAIS renvoyes au front-end
    api_key = serializers.CharField(write_only=True)
    api_secret = serializers.CharField(write_only=True)
    api_passphrase = serializers.CharField(write_only=True, required=False, allow_blank=True, default="")
    label = serializers.CharField(required=False, allow_blank=True, default="")
    # Version masquee pour l'affichage
    api_key_masked = serializers.SerializerMethodField()

    class Meta:
        model = ExchangeConnection
        fields = ["id", "exchange", "label", "api_key", "api_secret",
                  "api_passphrase", "api_key_masked"]
        read_only_fields = ["id", "api_key_masked"]

    def get_api_key_masked(self, obj):
        key = obj.api_key or ""
        if len(key) <= 8:
            return "****"
        return f"{key[:4]}...{key[-4:]}"

    def validate(self, attrs):
        exchange = attrs.get("exchange") or getattr(self.instance, "exchange", None)
        if exchange == "okx" and not attrs.get("api_passphrase"):
            raise serializers.ValidationError(
                {"api_passphrase": "Le passphrase est requis pour OKX."})
        return attrs
