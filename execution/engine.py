"""Moteur d'execution : accumulation sur support en discount + cloture multi-paliers.

Regles (Phase 5) :
1. Zones daily, seuils auto-ATR, toutes sauvegardees (fraiches = is_active).
2. Streak d'achats : buy_streak_schedule (defaut [2,2,1]).
3. Cassure rapide -> zone invalidee + position scellee.
4. ACHAT (pas de filtre tendance) : zone en discount (range as-of) ET
   - support confirme (touch_count>=2) -> achat direct,
   - sinon zone non confirmee -> pas de crash a l'approche (_bearish_strong)
     ET confirmation momentum a l'interieur (price action).
5. VENTE (TP) : a la resistance, momentum haussier fort -> on garde ; sinon on vend,
   ou si la zone casse a la baisse apres avoir ete atteinte.
   Le TP final = le plus haut connu au moment de l'achat (as-of) : l'echelle
   n'est donc JAMAIS vide quand on achete en discount.
6. Walkforward : uniquement les infos connues au moment t (zones first_seen<=t,
   range as-of, momentum as-of).
"""
from decimal import Decimal

from django.utils import timezone

from apps.exchanges.services import update_candles, place_spot_buy, place_spot_sell
from apps.exchanges.models import Candle
from apps.strategy.models import Zone
from apps.strategy.zone_detection import (
    detect_and_save_zones, price_in_zone, get_ordered_zones,
    find_target_support, build_resistance_ladder, compute_tp_percentages,
    is_price_action_confirmed,
)
from apps.notifications.services import notify
from .models import Trade, Position
from apps.strategy.zone_detection import SYMBOL_OVERRIDES

TIMEFRAME_PARAMS = {
    # min_swing_pct / movement_min_swing_pct absents -> auto-ATR dans zone_detection
    "1d": dict(tolerance_pct=0.6, left=8, right=8, min_band_pct=0.3),
}

DEFAULT_BUY_STREAK = [2.0, 2.0, 1.0]


def _refresh_zones(config):
    for tf, params in TIMEFRAME_PARAMS.items():
        update_candles(config.exchange, config.symbol, tf)
        detect_and_save_zones(config.exchange, config.symbol, tf, **params)


def _build_ladder_json(zones, entry_zone, top_price=None):
    """Echelle de TP : resistances au-dessus de l'entree + TP final = plus haut
    connu au moment de l'achat (as-of). Jamais vide si on achete en discount."""
    ladder_zones = build_resistance_ladder(zones, entry_zone)
    has_top = top_price is not None and top_price > entry_zone.price_high
    n = len(ladder_zones) + (1 if has_top else 0)
    percentages = compute_tp_percentages(n)
    steps = [{"zone_id": z.id, "percentage": pct, "closed": False}
             for z, pct in zip(ladder_zones, percentages)]
    if has_top:
        steps.append({"zone_id": None, "price": float(top_price),
                      "percentage": percentages[-1], "closed": False})
    return steps


def _current_atr(config, period=14):
    """ATR moyen des `period` dernieres bougies daily -> seuil d'eloignement (sealing)."""
    candles = list(Candle.objects.filter(
        exchange=config.exchange, symbol=config.symbol, timeframe="1d"
    ).order_by("-open_time")[:period + 1])
    if len(candles) < 2:
        return Decimal("0")
    trs = []
    for i in range(len(candles) - 1):
        c, prev = candles[i], candles[i + 1]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        trs.append(tr)
    return sum(trs, Decimal("0")) / Decimal(len(trs))


def _update_sealing(config, candle, notify_user=True):
    """Scelle l'accumulation des que le prix s'eloigne au-dela de
    zone_high + leave_atr_multiplier x ATR. Retour dans la zone = nouvelle position."""
    atr = _current_atr(config)
    if atr <= 0:
        return
    probe_price = candle.high
    for position in Position.objects.filter(
            strategy_config=config, is_fully_closed=False, is_sealed=False):
        zone = position.entry_zone
        leave_threshold = zone.price_high + atr * config.leave_atr_multiplier
        position.high_water_mark = max(position.high_water_mark, probe_price)
        if position.high_water_mark > leave_threshold:
            position.is_sealed = True
            if notify_user:
                notify(config.user,
                       f"[{config.mode.upper()}] {config.symbol} : prix parti au-dela de "
                       f"{leave_threshold:.2f}, accumulation scellee sur "
                       f"{zone.price_low}-{zone.price_high}")
        position.save(update_fields=["high_water_mark", "is_sealed", "updated_at"])


# =====================================================================
# Range as-of + discount + momentum
# =====================================================================

def _running_ranges(candles):
    """(lows, highs) cumules : range du graphique connu a chaque bougie, sans look-ahead."""
    lows, highs = [], []
    lo = hi = None
    for c in candles:
        lo = c.low if lo is None else min(lo, c.low)
        hi = c.high if hi is None else max(hi, c.high)
        lows.append(lo)
        highs.append(hi)
    return lows, highs


def _discount_ok(zone, swing_low, swing_high, discount_pct):
    """La zone doit etre dans le discount du range : son haut (pire cas d'entree)
    sous le niveau de retracement fib `discount_pct`. 75 = profond, 50 = equilibre."""
    if swing_low is None or swing_high is None:
        return True
    range_size = swing_high - swing_low
    if range_size <= 0:
        return False
    max_entry = swing_high - (discount_pct / Decimal("100")) * range_size
    return zone.price_high <= max_entry


def _bullish_strong(candles):
    """Momentum haussier fort : derniere bougie haussiere a grand corps.
    A la resistance, si True on NE vend pas (on laisse courir)."""
    if not candles:
        return False
    last = candles[-1]
    full_range = float(last.high) - float(last.low)
    if full_range == 0:
        return False
    body = float(last.close) - float(last.open)
    return body > 0 and (body / full_range) >= 0.5


def _bearish_strong(candles):
    """Momentum baissier fort (grosse bougie baissiere). A l'approche d'un support,
    si True = on fonce dans le couteau -> pas d'achat sur zone non confirmee."""
    if not candles:
        return False
    last = candles[-1]
    full_range = float(last.high) - float(last.low)
    if full_range == 0:
        return False
    body = float(last.close) - float(last.open)
    return body < 0 and (-body / full_range) >= 0.5


def _buy_allowed(zone, recent_candles, approach_candles):
    """Support confirme (a deja repousse le prix, touch_count>=2) -> achat direct.
    Sinon : pas de crash a l'approche ET confirmation momentum a l'interieur."""
    if zone.touch_count >= 2:
        return True
    if _bearish_strong(approach_candles):
        return False
    return is_price_action_confirmed(recent_candles)


def _buy_filters_ok(config, zone, swing_low, swing_high, recent_candles, approach_candles):
    """Discount (range as-of) + confirmation support/momentum. Pas de filtre tendance."""
    if not _discount_ok(zone, swing_low, swing_high, config.discount_pct):
        return False
    return _buy_allowed(zone, recent_candles, approach_candles)


def _check_fast_breaks(config, candle, notify_user=True):
    """Si le prix cloture sous la zone d'entree en <= fast_break_candles apres le
    1er achat -> cassure rapide : zone invalidee + position scellee."""
    for position in Position.objects.filter(
            strategy_config=config, is_fully_closed=False, is_sealed=False):
        if position.first_buy_at is None:
            continue
        zone = position.entry_zone
        if candle.close < zone.price_low:
            days_since = (candle.open_time - position.first_buy_at).days
            if days_since <= config.fast_break_candles:
                zone.is_active = False
                zone.save(update_fields=["is_active"])
                position.is_sealed = True
                position.save(update_fields=["is_sealed", "updated_at"])
                if notify_user:
                    notify(config.user,
                           f"[{config.mode.upper()}] {config.symbol} : cassure RAPIDE de "
                           f"{zone.price_low}-{zone.price_high} ({days_since}j apres entree) "
                           f"-> zone invalidee")


# =====================================================================
# Achats / Ventes
# =====================================================================

def _buy_into_position(config, zones, target_zone, current_price, executed_at,
                       notify_user=True, top_price=None):
    """Streak d'achats : % = buy_streak_schedule[buy_count] (defaut [2,2,1]).
    top_price = plus haut connu as-of -> TP final de l'echelle."""
    schedule = config.buy_streak_schedule or DEFAULT_BUY_STREAK
    if target_zone.is_validated and target_zone.percent_per_buy_override is not None:
        schedule = [float(target_zone.percent_per_buy_override)] * len(schedule)
    max_buys = len(schedule)

    position = (
        Position.objects.filter(strategy_config=config, entry_zone=target_zone, is_fully_closed=False)
        .order_by("-created_at").first()
    )
    if position is not None:
        if position.has_been_touched or position.is_sealed:
            position = None            # TP touche OU eloignement+retour -> nouvelle position
        elif position.buy_count >= max_buys:
            return                     # streak complet -> on attend

    buy_index = position.buy_count if position is not None else 0
    percent = Decimal(str(schedule[min(buy_index, len(schedule) - 1)]))
    amount_quote = config.balance_quote * (percent / Decimal("100"))

    if config.mode == "live":
        order = place_spot_buy(config.exchange_connection, config.symbol, float(amount_quote))
        amount_base = Decimal(str(order.get("filled") or order.get("amount")))
        order_id = order.get("id")
    else:
        amount_base = amount_quote / current_price
        order_id = None

    if position is None:
        ladder = _build_ladder_json(zones, target_zone, top_price=top_price)
        position = Position.objects.create(
            strategy_config=config, mode=config.mode, entry_zone=target_zone,
            entry_price=current_price, total_amount_base=Decimal("0"),
            total_quote_spent=Decimal("0"), resistance_ladder=ladder,
            buy_count=0, high_water_mark=current_price, first_buy_at=executed_at,
        )

    position.total_amount_base += amount_base
    position.total_quote_spent += amount_quote
    position.entry_price = position.total_quote_spent / position.total_amount_base
    position.buy_count += 1
    position.high_water_mark = max(position.high_water_mark, current_price)
    position.save(update_fields=["total_amount_base", "total_quote_spent",
                                 "entry_price", "buy_count", "high_water_mark", "updated_at"])

    Trade.objects.create(
        strategy_config=config, position=position, zone=target_zone, mode=config.mode, side="buy",
        executed_at=executed_at, price=current_price, amount_quote=amount_quote,
        amount_base=amount_base, exchange_order_id=order_id,
    )
    if notify_user:
        notify(config.user,
               f"[{config.mode.upper()}] Achat {config.symbol} ({percent}%) sur support "
               f"{target_zone.price_low}-{target_zone.price_high} : {amount_quote} @ {current_price} "
               f"({position.buy_count}/{max_buys})")


def _sell_step(config, position, zone, step, current_price, executed_at, notify_user):
    amount_to_sell = position.total_amount_base * (Decimal(str(step["percentage"])) / Decimal("100"))
    if config.mode == "live":
        order = place_spot_sell(config.exchange_connection, config.symbol, float(amount_to_sell))
        order_id = order.get("id")
    else:
        order_id = None
    Trade.objects.create(
        strategy_config=config, position=position, zone=zone, mode=config.mode, side="sell",
        executed_at=executed_at, price=current_price,
        amount_quote=amount_to_sell * current_price, amount_base=amount_to_sell,
        exchange_order_id=order_id,
    )
    step["closed"] = True
    if notify_user:
        notify(config.user, f"[{config.mode.upper()}] TP partiel {config.symbol} ({step['percentage']:.1f}%) "
                            f"sur {zone.price_low}-{zone.price_high} @ {current_price}")


def _process_tp_ladders(config, recent_candles, current_price, executed_at, notify_user=True):
    """TP conditionnel : a la resistance, momentum haussier fort -> on GARDE ; sinon on
    vend, ou si la zone casse a la baisse apres avoir ete atteinte. Gere aussi le
    palier synthetique (plus haut connu, sans zone)."""
    open_positions = Position.objects.filter(strategy_config=config, is_fully_closed=False)
    for position in open_positions:
        ladder = position.resistance_ladder
        changed = False
        for step in ladder:
            if step["closed"]:
                continue
            if step.get("zone_id"):
                try:
                    zone = position.entry_zone.__class__.objects.get(id=step["zone_id"])
                except position.entry_zone.__class__.DoesNotExist:
                    continue
                level = zone.price_low
            else:
                # Palier synthetique (plus haut connu au moment de l'achat)
                zone = position.entry_zone
                level = Decimal(str(step["price"]))

            if current_price >= level:
                # A la / au-dessus de la resistance
                step["reached"] = True
                if not _bullish_strong(recent_candles):
                    _sell_step(config, position, zone, step, current_price, executed_at, notify_user)
                    changed = True
                # sinon momentum fort -> on garde, on ne vend pas ce cycle
            else:
                if step.get("reached"):
                    # Atteinte avant, gardee, maintenant cassee a la baisse -> on vend
                    _sell_step(config, position, zone, step, current_price, executed_at, notify_user)
                    changed = True
                else:
                    break  # pas encore atteinte, les suivantes non plus
        if changed:
            position.resistance_ladder = ladder
            if ladder and all(s["closed"] for s in ladder):
                position.is_fully_closed = True
            position.save(update_fields=["resistance_ladder", "is_fully_closed", "updated_at"])


def _update_buy_target(config, zones, current_price):
    """Cible = toujours la zone la plus haute dont le bas est <= prix (elle suit le prix)."""
    new_target = find_target_support(zones, current_price)
    if new_target is None:
        return
    if config.target_zone is None or new_target.id != config.target_zone.id:
        if config.target_zone is not None and new_target.price_low < config.target_zone.price_low:
            notify(config.user, f"Support {config.target_zone.price_low}-{config.target_zone.price_high} casse "
                                f"sur {config.symbol}, nouvelle cible : {new_target.price_low}-{new_target.price_high}")
        config.target_zone = new_target
        config.save(update_fields=["target_zone"])



# =====================================================================
# Boucles principales
# =====================================================================

def run_test_or_live(config):
    """Appelee toutes les 4h par Celery Beat (zones daily uniquement)."""
    _refresh_zones(config)
    candles = list(Candle.objects.filter(
        exchange=config.exchange, symbol=config.symbol, timeframe="1d").order_by("open_time"))
    if not candles:
        return None
    last_candle = candles[-1]
    index = len(candles) - 1
    current_price = last_candle.close
    zones = get_ordered_zones(config.exchange, config.symbol)
    if not zones:
        return None
    run_lows, run_highs = _running_ranges(candles)
    recent = candles[-3:]
    approach = candles[-3:-1]  # bougies AVANT la bougie courante

    _process_tp_ladders(config, recent, current_price, timezone.now())
    _update_sealing(config, last_candle)
    _check_fast_breaks(config, last_candle)
    _update_buy_target(config, zones, current_price)

    if config.target_zone and price_in_zone(current_price, config.target_zone):
        if _buy_filters_ok(config, config.target_zone, run_lows[index], run_highs[index], recent, approach):
            _buy_into_position(config, zones, config.target_zone, current_price, timezone.now(),
                               top_price=run_highs[index])
    return config.trades.filter(mode=config.mode).order_by("-executed_at").first()


def run_walkforward(config, candles=None, verbose=False, notify_user=False):
    """Rejoue l'historique avec UNIQUEMENT les infos connues au moment t :
    zones as-of (first_seen <= t), range as-of, momentum as-of.
    
    Args:
        config: StrategyConfig instance
        candles: List[Candle] optionnelle. Si None, charge depuis la DB.
        verbose: Non utilisé (compatibilité).
        notify_user: Non utilisé (compatibilité).
    """
    _refresh_zones(config)
    
    # Utilise les bougies fournies ou charge depuis la DB
    if candles is None:
        candles = list(Candle.objects.filter(
            exchange=config.exchange, symbol=config.symbol, timeframe="1d"
        ).order_by("open_time"))
    
    # TOUTES les zones (actives + inactives) pour le as-of
    all_zones_db = list(Zone.objects.filter(exchange=config.exchange, symbol=config.symbol))
    if not all_zones_db:
        return config.trades.none()
    config.target_zone = None
    config.save(update_fields=["target_zone"])
    Position.objects.filter(strategy_config=config, mode="walkforward").delete()

    run_lows, run_highs = _running_ranges(candles)
    for i, candle in enumerate(candles):
        price = candle.close
        recent = candles[max(0, i - 2):i + 1]
        approach = candles[max(0, i - 2):i]  # avant la bougie courante
        # Zones connues au moment t
        zones = [z for z in all_zones_db if z.first_seen <= candle.open_time]
        if not zones:
            continue
        _process_tp_ladders(config, recent, price, candle.open_time, notify_user=False)
        _update_sealing(config, candle, notify_user=False)
        _check_fast_breaks(config, candle, notify_user=False)
        _update_buy_target(config, zones, price)
        if config.target_zone and price_in_zone(price, config.target_zone):
            if _buy_filters_ok(config, config.target_zone, run_lows[i], run_highs[i], recent, approach):
                _buy_into_position(config, zones, config.target_zone, price, candle.open_time,
                                   notify_user=False, top_price=run_highs[i])
    return config.trades.filter(mode="walkforward")
