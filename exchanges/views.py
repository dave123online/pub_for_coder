"""API des connexions exchange : CRUD + test des cles API."""
import ccxt
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from .models import ExchangeConnection
from .serializers import ExchangeConnectionSerializer
from .services import get_ccxt_client

from django.db.models import Min, Max, Count
from rest_framework.views import APIView
from .models import ExchangeConnection, Candle

def _test_connection(connection):
    """Tente un fetch_balance avec les cles. Retourne (status, detail) :
    'ok' | 'auth_error' | 'network_error' | 'error'."""
    try:
        client = get_ccxt_client(connection)
        client.load_markets()
        balance = client.fetch_balance()
        return "ok", balance
    except ccxt.AuthenticationError as e:
        return "auth_error", str(e)
    except ccxt.NetworkError as e:
        return "network_error", str(e)
    except Exception as e:
        return "error", str(e)


class ExchangeConnectionViewSet(viewsets.ModelViewSet):
    serializer_class = ExchangeConnectionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Chaque utilisateur ne voit QUE ses propres connexions
        return ExchangeConnection.objects.filter(user=self.request.user).order_by("-id")

    def perform_create(self, serializer):
        """Teste les cles AVANT de sauvegarder."""
        candidate = ExchangeConnection(user=self.request.user, **{
            k: serializer.validated_data[k]
            for k in ("exchange", "label", "api_key", "api_secret", "api_passphrase")
            if k in serializer.validated_data
        })
        test_status, result = _test_connection(candidate)
        if test_status == "auth_error":
            raise ValidationError({"api_key": "Cles refusees par l'exchange (authentification invalide)."})
        if test_status == "error":
            raise ValidationError({"detail": f"Impossible de tester la connexion : {result}"})
        # 'ok' ou 'network_error' -> on sauvegarde (l'exchange peut etre temporairement down)
        serializer.save(user=self.request.user)

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        """POST /connections/{id}/test/ -> verifie que les cles stockees fonctionnent."""
        connection = self.get_object()
        test_status, result = _test_connection(connection)
        if test_status == "ok":
            balances = {k: v for k, v in (result.get("total") or {}).items()
                        if v and float(v) > 0}
            return Response({"status": "ok", "balances": balances})
        return Response({"status": test_status, "detail": str(result)},
                        status=status.HTTP_400_BAD_REQUEST)

class AssetListView(APIView):
    """Liste les actifs pour lesquels on a de l'historique de bougies."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        timeframe = request.query_params.get("timeframe", "1d")
        exchange = request.query_params.get("exchange", "binance")

        assets = (
            Candle.objects.filter(exchange=exchange, timeframe=timeframe)
            .values("symbol")
            .annotate(
                first_candle=Min("open_time"),
                last_candle=Max("open_time"),
                candle_count=Count("id"),
            )
            .order_by("symbol")
        )

        result = []
        for a in assets:
            symbol = a["symbol"]
            base, _, quote = symbol.partition("/")
            history_days = (a["last_candle"] - a["first_candle"]).days
            result.append({
                "symbol": symbol,
                "base": base,
                "quote": quote,
                "timeframe": timeframe,
                "first_candle": a["first_candle"].date().isoformat(),
                "last_candle": a["last_candle"].date().isoformat(),
                "candle_count": a["candle_count"],
                "history_days": history_days,
                "has_sufficient_history": history_days >= 365,
            })

        return Response(result)
