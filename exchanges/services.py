"""Couche d'abstraction ccxt : fetch solde, fetch bougies incrémental, passage d'ordres."""
import ccxt
from django.utils import timezone
from django.utils.timezone import make_aware, is_naive
import datetime

from .models import Candle

TIMEFRAME_MS = {
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "1w": 7 * 24 * 60 * 60 * 1000,
}


def get_ccxt_client(connection):
    """Instancie un client ccxt à partir d'une ExchangeConnection (clés déjà déchiffrées à l'accès)."""
    exchange_class = getattr(ccxt, connection.exchange)
    params = {
        "apiKey": connection.api_key,
        "secret": connection.api_secret,
        "enableRateLimit": True,
    }
    if connection.exchange == "okx" and connection.api_passphrase:
        params["password"] = connection.api_passphrase
    return exchange_class(params)


def get_public_client(exchange_id):
    """Instancie un client ccxt PUBLIC (sans clés) pour fetcher les données de marché."""
    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({"enableRateLimit": True})


def fetch_balance(connection):
    client = get_ccxt_client(connection)
    return client.fetch_balance()


def update_candles(exchange_id, symbol, timeframe, since=None, fetch_all=False):
    """Fetch et sauvegarde les bougies.

    - fetch_all=False (defaut) : fetch incremental depuis la derniere bougie en DB.
    - fetch_all=True : fetch TOUT l'historique depuis le listing (pour le backtest).
    """
    ex = get_public_client(exchange_id)
    timeframe_ms = TIMEFRAME_MS.get(timeframe, ex.parse_timeframe(timeframe) * 1000)

    # Point de depart
    if fetch_all:
        since = since if since is not None else 0      # 0 = depuis le listing
    elif since is None:
        last = Candle.objects.filter(
            exchange=exchange_id, symbol=symbol, timeframe=timeframe
        ).order_by("-open_time").first()
        since = int(last.open_time.timestamp() * 1000) if last else 0

    total = 0
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        for ts, o, h, l, c, v in batch:
            Candle.objects.update_or_create(
                exchange=exchange_id, symbol=symbol, timeframe=timeframe,
                open_time=datetime.datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                defaults={"open": o, "high": h, "low": l, "close": c, "volume": v},
            )
        total += len(batch)
        last_ts = batch[-1][0]
        if last_ts <= since:            # anti-boucle infinie
            break
        since = last_ts + timeframe_ms
        if len(batch) < 1000:           # derniere page
            break
    return total


def place_spot_buy(connection, symbol, amount_quote):
    """Achat spot au marché pour un montant donné en devise de cotation (ex: USDT)."""
    client = get_ccxt_client(connection)
    ticker = client.fetch_ticker(symbol)
    amount_base = amount_quote / ticker["last"]
    return client.create_market_buy_order(symbol, amount_base)


def place_spot_sell(connection, symbol, amount_base):
    """Vente spot au marché pour une quantité donnée en devise de base (ex: BTC)."""
    client = get_ccxt_client(connection)
    return client.create_market_sell_order(symbol, amount_base)
