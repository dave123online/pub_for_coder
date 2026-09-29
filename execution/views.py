import math
from decimal import Decimal
from collections import defaultdict

from django.db.models import Sum
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import Position, Trade, BacktestRequest
from .serializers import (
    PositionSerializer,
    TradeSerializer,
    BacktestRequestSerializer,
)


class PositionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PositionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from apps.strategy.models import StrategyConfig
        configs = StrategyConfig.objects.filter(user=self.request.user)
        return Position.objects.filter(strategy_config__in=configs).order_by("-opened_at")


class TradeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TradeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from apps.strategy.models import StrategyConfig
        configs = StrategyConfig.objects.filter(user=self.request.user)
        return Trade.objects.filter(strategy_config__in=configs).order_by("-executed_at")


class PerformanceView(APIView):
    """Performance agrégée avec Sharpe Ratio et Max Drawdown."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.strategy.models import StrategyConfig

        strategy_id = request.query_params.get("strategy_config")
        configs = StrategyConfig.objects.filter(user=request.user)
        if strategy_id:
            configs = configs.filter(id=strategy_id)

        if not configs.exists():
            return Response({"detail": "Aucune stratégie trouvée."}, status=404)

        positions = Position.objects.filter(strategy_config__in=configs)
        trades = Trade.objects.filter(strategy_config__in=configs).order_by("executed_at")

        total_invested = positions.aggregate(total=Sum("total_quote_spent"))["total"] or 0
        open_positions = positions.filter(is_fully_closed=False).count()
        closed_positions = positions.filter(is_fully_closed=True).count()
        sealed_positions = positions.filter(is_sealed=True, is_fully_closed=False).count()

        buy_trades = trades.filter(side="buy")
        sell_trades = trades.filter(side="sell")
        total_buys = buy_trades.count()
        total_sells = sell_trades.count()
        total_quote_sold = sell_trades.aggregate(total=Sum("amount_quote"))["total"] or 0

        closed_with_pnl = []
        for pos in positions.filter(is_fully_closed=True).prefetch_related("trades"):
            sold_quote = sum(t.amount_quote for t in pos.trades.all() if t.side == "sell")
            pnl = sold_quote - pos.total_quote_spent
            closed_with_pnl.append(pnl)

        wins = sum(1 for p in closed_with_pnl if p > 0)
        total_closed = len(closed_with_pnl)
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0
        realized_pnl = sum(closed_with_pnl)

        max_drawdown_pct, sharpe_ratio = self._calculate_advanced_metrics(trades)

        return Response({
            "strategy_config": int(strategy_id) if strategy_id else None,
            "total_invested": float(total_invested),
            "total_recovered_from_sells": float(total_quote_sold),
            "realized_pnl": float(realized_pnl),
            "realized_pnl_pct": float(realized_pnl / total_invested * 100) if total_invested else 0,
            "total_buys": total_buys,
            "total_sells": total_sells,
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            "sealed_positions": sealed_positions,
            "win_rate_pct": round(win_rate, 1),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": round(sharpe_ratio, 2),
        })

    def _calculate_advanced_metrics(self, trades):
        if not trades.exists():
            return 0.0, 0.0

        equity = Decimal("0")
        equity_curve = []
        for trade in trades:
            if trade.side == "buy":
                equity -= trade.amount_quote
            elif trade.side == "sell":
                equity += trade.amount_quote
            equity_curve.append(float(equity))

        if len(equity_curve) < 2:
            return 0.0, 0.0

        peak = equity_curve[0]
        max_drawdown = 0.0
        for value in equity_curve:
            if value > peak:
                peak = value
            drawdown = peak - value
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        initial_capital = abs(equity_curve[0]) if equity_curve[0] != 0 else 1
        max_drawdown_pct = (max_drawdown / initial_capital * 100) if initial_capital > 0 else 0

        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] != 0:
                ret = (equity_curve[i] - equity_curve[i - 1]) / abs(equity_curve[i - 1])
                returns.append(ret)

        if len(returns) < 2:
            return max_drawdown_pct, 0.0

        avg_return = sum(returns) / len(returns)
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)
        sharpe_ratio = (avg_return / std_dev) * math.sqrt(365) if std_dev > 0 else 0.0

        return max_drawdown_pct, sharpe_ratio


class BacktestViewSet(viewsets.ModelViewSet):
    serializer_class = BacktestRequestSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete"]

    def get_queryset(self):
        return BacktestRequest.objects.filter(user=self.request.user).order_by("-created_at")

    def perform_create(self, serializer):
        from .tasks import run_client_backtest
        bt = serializer.save(user=self.request.user)
        task = run_client_backtest.delay(bt.id)
        bt.celery_task_id = task.id
        bt.save(update_fields=["celery_task_id"])


class PortfolioSimulationView(APIView):
    """Simulation portefeuille ARIMA : paramètres internes fixes."""
    permission_classes = [IsAuthenticated]

    AVAILABLE_SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
        "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "MATIC/USDT",
    ]
    INTERNAL_DISCOUNT_PCT = "50"

    def get(self, request):
        batch_ids = request.query_params.getlist("ids")
        if not batch_ids:
            return Response({"detail": "Paramètre 'ids' requis."}, status=400)

        # Capital total = somme des capitals individuels alloués
        total_portfolio_capital = sum(
            float(bt.capital) for bt in BacktestRequest.objects.filter(id__in=batch_ids)
        )

        bts = BacktestRequest.objects.filter(id__in=batch_ids, user=self.request.user)
        results = []
        all_completed = True

        # Agréger les courbes d'équité par date
        aggregated_equity = defaultdict(float)
        
        for bt in bts:
            if bt.status != "completed":
                all_completed = False
            r = bt.results or {}
            
            # Récupérer la courbe d'équité si disponible
            if "equity_curve" in r and "equity_dates" in r:
                equity_curve = r["equity_curve"]
                equity_dates = r["equity_dates"]
                
                # Ajouter à la courbe agrégée
                for i, (date, equity) in enumerate(zip(equity_dates, equity_curve)):
                    if i == 0:
                        continue  # Sauter le point initial
                    # On ajoute la variation depuis le capital initial de cet actif
                    variation = equity - float(bt.capital)
                    aggregated_equity[date] += variation
            
            results.append({
                "id": bt.id,
                "symbol": bt.symbol,
                "status": bt.status,
                "capital_allocated": float(bt.capital),
                
                "realized_pnl": r.get("realized_pnl", 0),
                "realized_pnl_pct": r.get("realized_pnl_pct", 0),
                "unrealized_pnl": r.get("unrealized_pnl", 0),
                "unrealized_pnl_pct": r.get("unrealized_pnl_pct", 0),
                "total_pnl": r.get("total_pnl", 0),
                "total_pnl_pct": r.get("total_pnl_pct", 0),
                
                "win_rate_pct": r.get("win_rate_pct", 0),
                "win_rate_sample_size": r.get("win_rate_sample_size", 0),
                "max_drawdown_pct": r.get("max_drawdown_pct", 0),
                "sharpe_ratio": r.get("sharpe_ratio", 0),
                "total_buys": r.get("total_buys", 0),
                "total_sells": r.get("total_sells", 0),
                "candles_analyzed": r.get("candles_analyzed", 0),
                "open_positions": r.get("open_positions", 0),
                "closed_positions": r.get("closed_positions", 0),
                "invested_in_open": r.get("invested_in_open", 0),
            })

        results.sort(key=lambda x: x["total_pnl"], reverse=True)
        completed = [r for r in results if r["status"] == "completed"]

        # === RECALCUL DES MÉTRIQUES AGRÉGÉES ===
        total_pnl = sum(r["total_pnl"] for r in completed)
        total_realized = sum(r["realized_pnl"] for r in completed)
        total_unrealized = sum(r["unrealized_pnl"] for r in completed)
        
        # P&L% relatif au capital TOTAL du portefeuille
        total_pnl_pct = (total_pnl / total_portfolio_capital * 100) if total_portfolio_capital > 0 else 0
        total_realized_pct = (total_realized / total_portfolio_capital * 100) if total_portfolio_capital > 0 else 0
        total_unrealized_pct = (total_unrealized / total_portfolio_capital * 100) if total_portfolio_capital > 0 else 0
        
        # Construire la courbe d'équité agrégée
        sorted_dates = sorted(aggregated_equity.keys())
        portfolio_equity_curve = [total_portfolio_capital]
        for date in sorted_dates:
            portfolio_equity_curve.append(total_portfolio_capital + aggregated_equity[date])
        
        # Calculer Max Drawdown et Sharpe sur la courbe agrégée
        portfolio_max_dd_pct = 0.0
        portfolio_sharpe = 0.0
        
        if len(portfolio_equity_curve) >= 2:
            # Max Drawdown
            peak = portfolio_equity_curve[0]
            max_dd = 0.0
            for val in portfolio_equity_curve:
                if val > peak:
                    peak = val
                dd = peak - val
                if dd > max_dd:
                    max_dd = dd
            portfolio_max_dd_pct = round((max_dd / total_portfolio_capital) * 100, 2) if total_portfolio_capital > 0 else 0.0
            
            # Sharpe Ratio
            returns = []
            for i in range(1, len(portfolio_equity_curve)):
                if portfolio_equity_curve[i - 1] != 0:
                    ret = (portfolio_equity_curve[i] - portfolio_equity_curve[i - 1]) / abs(portfolio_equity_curve[i - 1])
                    returns.append(ret)
            
            if len(returns) >= 2:
                avg_r = sum(returns) / len(returns)
                var_r = sum((r - avg_r) ** 2 for r in returns) / len(returns)
                std_r = math.sqrt(var_r)
                portfolio_sharpe = round((avg_r / std_r) * math.sqrt(365), 2) if std_r > 0 else 0.0

        return Response({
            "status": "completed" if all_completed else "running",
            "progress": f"{len(completed)}/{len(results)}",
            "portfolio_summary": {
                "total_assets": len(completed),
                "total_capital": round(total_portfolio_capital, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round(total_pnl_pct, 2),
                "total_realized_pnl": round(total_realized, 2),
                "total_realized_pct": round(total_realized_pct, 2),
                "total_unrealized_pnl": round(total_unrealized, 2),
                "total_unrealized_pct": round(total_unrealized_pct, 2),
                "sharpe_ratio": round(portfolio_sharpe, 2),
                "max_drawdown_pct": round(portfolio_max_dd_pct, 2),
                "best_performer": completed[0]["symbol"] if completed else None,
                "worst_performer": completed[-1]["symbol"] if completed else None,
            },
            "rankings": results,
        })

    def post(self, request):
        from .tasks import run_client_backtest

        symbols = request.data.get("symbols", [])
        capital = request.data.get("capital", "1000")
        start_date = request.data.get("start_date", "2020-01-01")

        if not symbols or not isinstance(symbols, list):
            return Response({"detail": "Le champ 'symbols' doit être une liste non vide."}, status=400)

        valid_symbols = [s for s in symbols if s in self.AVAILABLE_SYMBOLS]
        if not valid_symbols:
            return Response({"detail": "Aucun actif valide sélectionné.", "available": self.AVAILABLE_SYMBOLS}, status=400)

        # === DIVISION DU CAPITAL SUR LES ACTIFS ===
        total_capital = Decimal(str(capital))
        capital_per_asset = total_capital / Decimal(str(len(valid_symbols)))
        capital_per_asset = capital_per_asset.quantize(Decimal("0.01"))

        batch_ids = []
        for symbol in valid_symbols:
            bt = BacktestRequest.objects.create(
                user=request.user,
                exchange="binance",
                symbol=symbol,
                capital=capital_per_asset,
                start_date=start_date,
                discount_pct=self.INTERNAL_DISCOUNT_PCT,
            )
            task = run_client_backtest.delay(bt.id)
            bt.celery_task_id = task.id
            bt.save(update_fields=["celery_task_id"])
            batch_ids.append(bt.id)

        return Response({
            "batch_ids": batch_ids,
            "symbols": valid_symbols,
            "capital_per_asset": float(capital_per_asset),
            "total_capital": float(total_capital),
            "status": "running",
        }, status=status.HTTP_201_CREATED)
