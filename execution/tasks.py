"""Tâche Celery : backtest client asynchrone."""
import math
from decimal import Decimal
from datetime import datetime

from celery import shared_task
from django.utils import timezone


@shared_task(bind=True, max_retries=1)
def run_client_backtest(self, backtest_id):
    from .models import BacktestRequest, Position, Trade
    from apps.strategy.models import StrategyConfig
    from apps.exchanges.models import Candle
    from .engine import run_walkforward

    bt = BacktestRequest.objects.get(id=backtest_id)
    bt.status = "running"
    bt.celery_task_id = self.request.id or ""
    bt.save(update_fields=["status", "celery_task_id"])

    config = None
    try:
        # 1. Config temporaire (is_active=False pour ne pas polluer la liste client)
        config = StrategyConfig.objects.create(
            user=bt.user,
            exchange=bt.exchange,
            symbol=bt.symbol,
            mode="walkforward",
            balance_quote=bt.capital,
            discount_pct=bt.discount_pct,
            is_active=False,
        )

        # 2. Bougies filtrées depuis start_date
        candles_qs = Candle.objects.filter(
            exchange=bt.exchange, symbol=bt.symbol, timeframe="1d"
        ).order_by("open_time")
        if bt.start_date:
            candles_qs = candles_qs.filter(open_time__date__gte=bt.start_date)
        candles = list(candles_qs)

        if len(candles) < 365:
            raise ValueError(f"Historique insuffisant : {len(candles)} bougies (min 365).")

        # 3. Lance le walkforward
        run_walkforward(config, candles=candles, verbose=False, notify_user=False)

        # 4. Extrait les résultats AVANT de supprimer la config
        positions = Position.objects.filter(strategy_config=config)
        trades = Trade.objects.filter(strategy_config=config)
        sells = trades.filter(side="sell")

        # === STATISTIQUES SÉPARÉES : RÉALISÉ vs NON RÉALISÉ ===
        
        # Positions fermées
        closed_positions = [p for p in positions if p.is_fully_closed]
        open_positions = [p for p in positions if not p.is_fully_closed]
        
        # P&L RÉALISÉ (positions fermées uniquement)
        realized_pnl = Decimal("0")
        wins = 0
        for pos in closed_positions:
            # Somme des ventes pour cette position
            sold_quote = sum(
                (t.amount_quote for t in pos.trades.all() if t.side == "sell"),
                Decimal("0")
            )
            pnl = sold_quote - pos.total_quote_spent
            realized_pnl += pnl
            if pnl > 0:
                wins += 1
        
        # P&L NON RÉALISÉ (positions ouvertes valorisées au dernier prix)
        unrealized_pnl = Decimal("0")
        if candles:
            last_price = candles[-1].close
            for pos in open_positions:
                # Valeur actuelle - coût d'achat
                current_value = pos.total_amount_base * last_price
                unrealized_pnl += (current_value - pos.total_quote_spent)
        
        # P&L TOTAL (réalisé + non réalisé)
        total_pnl = realized_pnl + unrealized_pnl
        
        # Capital investi dans les positions ouvertes
        invested_in_open = sum((p.total_quote_spent for p in open_positions), Decimal("0"))
        
        # Win rate sur positions FERMÉES uniquement
        win_rate = (wins / len(closed_positions) * 100) if len(closed_positions) > 0 else 0.0
        
        # Capital total investi (fermées + ouvertes)
        total_invested = sum((p.total_quote_spent for p in positions), Decimal("0"))
        total_recovered = sum((t.amount_quote for t in sells), Decimal("0"))

        bt.results = {
            "symbol": bt.symbol,
            "capital": float(bt.capital),
            "start_date": str(bt.start_date) if bt.start_date else None,
            "candles_analyzed": len(candles),
            "total_buys": trades.filter(side="buy").count(),
            "total_sells": sells.count(),
            
            # Stats séparées
            "realized_pnl": float(realized_pnl),
            "realized_pnl_pct": float(realized_pnl / bt.capital * 100) if bt.capital else 0.0,
            "unrealized_pnl": float(unrealized_pnl),
            "unrealized_pnl_pct": float(unrealized_pnl / invested_in_open * 100) if invested_in_open else 0.0,
            "total_pnl": float(total_pnl),
            "total_pnl_pct": float(total_pnl / bt.capital * 100) if bt.capital else 0.0,
            
            # Positions
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "invested_in_open": float(invested_in_open),
            
            # Win rate sur positions fermées uniquement
            "win_rate_pct": round(win_rate, 1),
            "win_rate_sample_size": len(closed_positions),
        }

        # === CALCUL SHARPE & MAX DRAWDOWN (CORRECT) ===
        initial_capital = float(config.balance_quote)
        
        all_trades = list(Trade.objects.filter(
            strategy_config=config
        ).order_by("executed_at"))
        
        trades_by_date = {}
        for trade in all_trades:
            day = trade.executed_at.date()
            if day not in trades_by_date:
                trades_by_date[day] = []
            trades_by_date[day].append(trade)
        
        equity_curve = [initial_capital]
        equity_dates = [candles[0].open_time.date().isoformat() if candles else ""]
        cash = Decimal(str(initial_capital))
        positions_held = []
        
        for candle in candles:
            day = candle.open_time.date()
            close_price = float(candle.close)
            
            if day in trades_by_date:
                for trade in trades_by_date[day]:
                    if trade.side == "buy":
                        cash -= trade.amount_quote
                        positions_held.append((float(trade.amount_base), float(trade.price)))
                    elif trade.side == "sell":
                        cash += trade.amount_quote
                        amount_to_sell = float(trade.amount_base)
                        for i in range(len(positions_held)):
                            if amount_to_sell <= 0:
                                break
                            held_amount, entry_price = positions_held[i]
                            if held_amount <= amount_to_sell:
                                amount_to_sell -= held_amount
                                positions_held[i] = (0, entry_price)
                            else:
                                positions_held[i] = (held_amount - amount_to_sell, entry_price)
                                amount_to_sell = 0
                        positions_held = [(amt, price) for amt, price in positions_held if amt > 0]
            
            positions_value = sum(amount * close_price for amount, _ in positions_held)
            equity = float(cash) + positions_value
            equity_curve.append(equity)
            equity_dates.append(day.isoformat())

        max_dd_pct = 0.0
        sharpe = 0.0

        if len(equity_curve) >= 2:
            peak = equity_curve[0]
            max_dd = 0.0
            for val in equity_curve:
                if val > peak:
                    peak = val
                dd = peak - val
                if dd > max_dd:
                    max_dd = dd
            max_dd_pct = round((max_dd / initial_capital) * 100, 2) if initial_capital > 0 else 0.0
            max_dd_pct = min(max_dd_pct, 100.0)

            returns = []
            for i in range(1, len(equity_curve)):
                if equity_curve[i - 1] != 0:
                    ret = (equity_curve[i] - equity_curve[i - 1]) / abs(equity_curve[i - 1])
                    returns.append(ret)

            if len(returns) >= 2:
                avg_r = sum(returns) / len(returns)
                var_r = sum((r - avg_r) ** 2 for r in returns) / len(returns)
                std_r = math.sqrt(var_r)
                sharpe = round((avg_r / std_r) * math.sqrt(365), 2) if std_r > 0 else 0.0

        bt.results["max_drawdown_pct"] = max_dd_pct
        bt.results["sharpe_ratio"] = sharpe
        bt.results["equity_curve"] = equity_curve
        bt.results["equity_dates"] = equity_dates

        bt.status = "completed"
        bt.completed_at = timezone.now()
        bt.save()

        # 5. Nettoie la config temporaire (cascade sur positions/trades)
        config.delete()

    except Exception as e:
        bt.status = "failed"
        bt.error = str(e)
        bt.save()
        if config:
            config.delete()
        raise
