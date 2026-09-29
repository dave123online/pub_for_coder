"""Détection de zones de support/résistance :

1. Détection de pivots (swing highs/lows) sur une fenêtre glissante
2. Clustering des pivots proches en prix -> zones (avec dédoublonnage temporel)
3. Scoring par nombre de touches + ancienneté
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import List

from apps.exchanges.models import Candle
from .models import Zone


# =====================================================================
# Seuils de swing auto-adaptatifs (hybride : base ATR + overrides manuels)
# =====================================================================

# Référence de calibration : seuils VALIDÉS par timeframe (BTC daily = 4 zones), associés
# à l'ATR% typique de BTC. Tout autre actif est mis à l'échelle par ratio d'ATR.
# Si le rendu BTC s'écarte des ~4 zones, ajuste "atr_pct" ci-dessous (visualize_zones.py
# affiche l'ATR% réel de l'actif).
_SWING_REF = {
    # timeframe: (atr_pct_ref, min_swing_ref)
    "4h": (1.2, 8.0),
    "1d": (2.46, 20.0),  # ← recalibré avec l'ATR% réel de BTC
    "1w": (6.0, 15.0),
}
# Ratio movement/min_swing validé sur BTC (35 / 20)
_MOVEMENT_RATIO = 1.75

# Overrides manuels par actif (le volet "manuel" de l'hybride) : remplacent le calcul
# ATR-auto pour l'actif concerné. Utile pour un profil de volatilité atypique.
SYMBOL_OVERRIDES = {
    # BTC/SOL : on garde uniquement les seuils de détection, pas le range
    "BTC/USDT": {"min_swing_pct": 15.0, "movement_min_swing_pct": 30.0},
    "SOL/USDT": {"min_swing_pct": 18.0, "movement_min_swing_pct": 30.0},
}


def _norm_price(x):
    """Normalise un prix pour que update_or_create matche d'un run a l'autre."""
    return Decimal(f"{float(x):.8g}")


def _atr_percent(candles, period=60):
    """ATR en % du prix (médiane des `period` dernières bougies) → volatilité typique
    de l'actif sur ce timeframe. Médiane pour résister aux bougies extrêmes."""
    recent = candles[-(period + 1):]
    if len(recent) < 2:
        return None
    trs = []
    for i in range(1, len(recent)):
        c, prev = recent[i], recent[i - 1]
        if prev.close <= 0:
            continue
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        trs.append(float(tr / prev.close * 100))
    if not trs:
        return None
    trs.sort()
    return trs[len(trs) // 2]


# Bornes pour le daily : évite les extrêmes sur actifs très stables ou très volatils
_SWING_BOUNDS = {
    "1d": (15.0, 45.0),  # (floor, ceil) en %
}


def auto_swing_thresholds(candles, timeframe):
    """(min_swing_pct, movement_min_swing_pct) adaptés à la volatilité de l'actif,
    calibrés pour retrouver le rendu validé sur BTC. (None, None) si pas assez de données."""
    ref = _SWING_REF.get(timeframe)
    if ref is None:
        return None, None
    atr_pct = _atr_percent(candles)
    if atr_pct is None:
        return None, None
    ref_atr, ref_swing = ref
    scale = atr_pct / ref_atr
    min_swing = ref_swing * scale
    
    # Appliquer les bornes (évite TRX à 9.9% ou UNI à 49%)
    bounds = _SWING_BOUNDS.get(timeframe)
    if bounds:
        floor, ceil = bounds
        min_swing = max(floor, min(ceil, min_swing))
    
    return min_swing, min_swing * _MOVEMENT_RATIO

@dataclass
class Pivot:
    open_time: object
    price: Decimal
    kind: str  # "high" ou "low"
    local_atr: Decimal  # volatilite TYPIQUE autour du pivot (bougies voisines, pivot exclu)


def _local_atr(candles: List[Candle], index: int, period: int = 14) -> Decimal:
    """Range moyen (high-low) des `period` bougies PRECEDANT le pivot (le pivot lui-meme est
    exclu expres : un pivot est par nature une bougie extreme/anormalement volatile, puisque
    c'est souvent cette volatilite qui cree le swing - l'inclure biaiserait la mesure vers le
    haut). Represente la volatilite "typique" autour du pivot, pas celle du pivot lui-meme."""
    start = max(0, index - period)
    window = candles[start:index]
    if not window:
        return candles[index].high - candles[index].low
    ranges = [w.high - w.low for w in window]
    return sum(ranges) / len(ranges)


def find_pivots(candles: List[Candle], left: int = 10, right: int = 10, atr_period: int = 14) -> List[Pivot]:
    """Un pivot high/low est un point dont le high/low est le plus extrême sur [i-left, i+right].
    left/right plus grands = pivots plus significatifs, moins de bruit (à ajuster par timeframe :
    plus petit en H4, plus grand en daily/weekly)."""
    pivots = []
    for i in range(left, len(candles) - right):
        window = candles[i - left:i + right + 1]
        c = candles[i]
        atr = _local_atr(candles, i, period=atr_period)
        if c.high == max(w.high for w in window):
            pivots.append(Pivot(c.open_time, c.high, "high", atr))
        if c.low == min(w.low for w in window):
            pivots.append(Pivot(c.open_time, c.low, "low", atr))
    return pivots


def _dedupe_close_in_time(cluster: List[Pivot], min_gap_days: float) -> List[Pivot]:
    """Au sein d'un cluster de prix, fusionne les pivots trop proches dans le temps :
    ils représentent probablement la même oscillation, pas deux réactions distinctes du marché."""
    if min_gap_days <= 0:
        return cluster
    ordered = sorted(cluster, key=lambda p: p.open_time)
    kept = [ordered[0]]
    for p in ordered[1:]:
        gap = (p.open_time - kept[-1].open_time).total_seconds() / 86400
        if gap >= min_gap_days:
            kept.append(p)
        # sinon : ignoré, trop proche dans le temps du précédent déjà gardé
    return kept


def cluster_pivots(pivots: List[Pivot], tolerance_pct: float = 0.5, min_gap_days: float = 0) -> List[dict]:
    """Regroupe les pivots dont les prix sont proches (tolérance en %) en zones.
    min_gap_days : écart minimum entre deux touches pour qu'elles comptent comme des réactions
    distinctes du marché (évite qu'une seule oscillation ne gonfle artificiellement touch_count)."""
    pivots_sorted = sorted(pivots, key=lambda p: p.price)
    clusters = []
    current = []

    for p in pivots_sorted:
        if not current:
            current = [p]
            continue
        ref_price = current[-1].price
        if abs(float(p.price) - float(ref_price)) / float(ref_price) * 100 <= tolerance_pct:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    if current:
        clusters.append(current)

    zones = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue  # un seul pivot = pas de largeur reelle, pas une zone exploitable
            # (les points isoles significatifs sont geres a part par ensure_extreme_zones)
        deduped = _dedupe_close_in_time(cluster, min_gap_days)
        prices = [c.price for c in cluster]  # largeur de zone basée sur tous les pivots du cluster
        zones.append({
            "price_low": min(prices),
            "price_high": max(prices),
            "touch_count": len(deduped),
            "first_seen": min(c.open_time for c in cluster),
            "last_touched": max(c.open_time for c in cluster),
        })
    return zones


def score_zone(zone: dict) -> float:
    """Score simple : plus de touches distinctes = zone plus significative."""
    return zone["touch_count"] * 1.0


def detect_and_save_zones(exchange: str, symbol: str, timeframe: str,
                          tolerance_pct: float = 0.5, left: int = 10, right: int = 10,
                          min_band_pct: float = 0.3, min_swing_pct: float = None,
                          movement_min_swing_pct: float = None, **_legacy_ignored):
    """Sauvegarde TOUTES les zones detectees (pour le walkforward as-of), mais ne
    marque is_active=True que pour les fraiches (3 intermediaires recentes + extremes).
    Les zones manuelles ne sont jamais touchees."""
    candles = list(Candle.objects.filter(exchange=exchange, symbol=symbol, timeframe=timeframe).order_by("open_time"))
    if len(candles) < (left + right + 5):
        return []

    # --- Resolution des seuils ---
    override = SYMBOL_OVERRIDES.get(symbol, {})
    if min_swing_pct is None:
        min_swing_pct = override.get("min_swing_pct")
    if movement_min_swing_pct is None and "movement_min_swing_pct" in override:
        movement_min_swing_pct = override["movement_min_swing_pct"]
    if min_swing_pct is None or movement_min_swing_pct is None:
        a_swing, a_movement = auto_swing_thresholds(candles, timeframe)
        if min_swing_pct is None:
            min_swing_pct = a_swing if a_swing is not None else 3.0
        if movement_min_swing_pct is None:
            movement_min_swing_pct = a_movement

    all_zones = detect_structure_zones(candles, left=left, right=right, tolerance_pct=tolerance_pct,
                                       min_band_pct=min_band_pct, min_swing_pct=min_swing_pct,
                                       movement_min_swing_pct=movement_min_swing_pct)

    # --- Zones fraiches = 3 intermediaires les plus recentes ---
    MAX_INTERMEDIATE_ZONES = 3
    recent = sorted(all_zones, key=lambda z: z["last_touched"], reverse=True)[:MAX_INTERMEDIATE_ZONES]
    recent_keys = {(_norm_price(z["price_low"]), _norm_price(z["price_high"])) for z in recent}

    # --- Sauvegarde TOUTES les zones (is_active = fraiche ou non) ---
    saved_all = []
    recent_objs = []
    for z in all_zones:
        is_fresh = (_norm_price(z["price_low"]), _norm_price(z["price_high"])) in recent_keys
        obj, _ = Zone.objects.update_or_create(
            exchange=exchange, symbol=symbol, timeframe=timeframe,
                        price_low=_norm_price(z["price_low"]), price_high=_norm_price(z["price_high"]),
            defaults={
                "touch_count": z["touch_count"],
                "score": score_zone(z),
                "first_seen": z["first_seen"],
                "last_touched": z["last_touched"],
                "is_active": is_fresh,
            },
        )
        saved_all.append(obj)
        if is_fresh:
            recent_objs.append(obj)

    # --- Extremes toujours actifs ---
    extras = ensure_extreme_zones(candles, recent_objs, tolerance_pct=tolerance_pct)
    for extra in extras:
        obj, _ = Zone.objects.update_or_create(
            exchange=exchange, symbol=symbol, timeframe=timeframe,
                        price_low=_norm_price(extra["price_low"]), price_high=_norm_price(extra["price_high"]),
            defaults={
                "touch_count": extra["touch_count"],
                "score": score_zone(extra),
                "first_seen": extra["first_seen"],
                "last_touched": extra["last_touched"],
                "is_active": True,
            },
        )
        saved_all.append(obj)

    return saved_all

def ensure_extreme_zones(candles, existing_zones, tolerance_pct=0.5, max_atr_mult=2.0):
    """Garantit que le plus haut et le plus bas absolus sont couverts, mais avec une
    hauteur plafonnee a max_atr_mult × ATR recent : une bougie extreme au range enorme
    (crash, listing) ne doit pas creer une zone haute de plusieurs dollars, inutilisable
    par l'engine (price_in_zone serait True sur toute la plage)."""
    if not candles:
        return []

    highest = max(candles, key=lambda c: c.high)
    lowest = min(candles, key=lambda c: c.low)

    # ATR global recent (mediane des ranges des 60 dernieres bougies)
    recent = candles[-60:]
    ranges = sorted(float(c.high) - float(c.low) for c in recent)
    atr = ranges[len(ranges) // 2] if ranges else 0.0
    max_height = atr * max_atr_mult

    extra = []
    for extreme_candle, kind in [(highest, "high"), (lowest, "low")]:
        price = float(extreme_candle.high if kind == "high" else extreme_candle.low)
        already_covered = any(
            float(z.price_low) * (1 - tolerance_pct / 100) <= price <= float(z.price_high) * (1 + tolerance_pct / 100)
            for z in existing_zones)
        if not already_covered:
            low, high = float(extreme_candle.low), float(extreme_candle.high)
            if max_height > 0 and (high - low) > max_height:
                if kind == "high":
                    low = high - max_height   # on garde le sommet, on rogne le bas
                else:
                    high = low + max_height   # on garde le plancher, on rogne le haut
            extra.append({
                "price_low": low,
                "price_high": high,
                "touch_count": 1,
                "first_seen": extreme_candle.open_time,
                "last_touched": extreme_candle.open_time,
            })
    return extra


def merge_overlapping_zones(zones: List[dict]) -> List[dict]:
    """Fusionne les zones (dicts) dont les fourchettes de prix se chevauchent, quelle que soit
    leur source (range de consolidation, cluster de pivots, extreme garanti). Evite les doublons
    quasi-identiques quand plusieurs methodes detectent la meme zone reelle."""
    if not zones:
        return []

    ordered = sorted(zones, key=lambda z: z["price_low"])
    merged = [dict(ordered[0])]

    for z in ordered[1:]:
        last = merged[-1]
        if z["price_low"] <= last["price_high"]:  # chevauchement
            last["price_low"] = min(last["price_low"], z["price_low"])
            last["price_high"] = max(last["price_high"], z["price_high"])
            last["touch_count"] = max(last["touch_count"], z["touch_count"])  # evite de sur-compter les doublons
            last["first_seen"] = min(last["first_seen"], z["first_seen"])
            last["last_touched"] = max(last["last_touched"], z["last_touched"])
        else:
            merged.append(dict(z))

    return merged


def detect_frequency_zones(candles, num_bins=50, min_touches=10):
    """Detecte les zones larges ou le prix revient souvent, meme sans y etre confine
    etroitement (contrairement a detect_range_zones qui exige un plateau serre).
    Principe : histogramme du nombre de fois ou le prix (high ou low de chaque bougie)
    tombe dans chaque tranche de prix ; les tranches avec beaucoup de passages, une fois
    fusionnees si contigues, deviennent des zones larges de "prix frequente"."""
    if not candles:
        return []

    all_prices = [float(c.high) for c in candles] + [float(c.low) for c in candles]
    price_min, price_max = min(all_prices), max(all_prices)
    if price_max <= price_min:
        return []

    bin_width = (price_max - price_min) / num_bins
    counts = [0] * num_bins
    first_seen = [None] * num_bins
    last_touched = [None] * num_bins

    def bin_index(p):
        idx = int((p - price_min) / bin_width)
        return min(idx, num_bins - 1)

    for c in candles:
        touched_bins = {bin_index(float(c.high)), bin_index(float(c.low))}
        for idx in touched_bins:
            counts[idx] += 1
            if first_seen[idx] is None:
                first_seen[idx] = c.open_time
            last_touched[idx] = c.open_time

    # fusionne les tranches contigues qui depassent le seuil de touches en zones larges
    zones = []
    i = 0
    while i < num_bins:
        if counts[i] < min_touches:
            i += 1
            continue
        j = i
        total_touches = 0
        fs, lt = first_seen[i], last_touched[i]
        while j < num_bins and counts[j] >= min_touches:
            total_touches += counts[j]
            fs = min(fs, first_seen[j])
            lt = max(lt, last_touched[j])
            j += 1
        zones.append({
            "price_low": price_min + i * bin_width,
            "price_high": price_min + j * bin_width,
            "touch_count": total_touches,
            "first_seen": fs,
            "last_touched": lt,
        })
        i = j

    return zones


def detect_structure_zones(candles, left=10, right=10, tolerance_pct=0.5, min_band_pct=0.3,
                            min_swing_pct=3.0, movement_min_swing_pct=None):
    """Detection par structure de mouvement : chaque swing high/low (pivot) est un point de
    structure - le bas d'une correction dans un mouvement haussier, le haut d'une correction
    dans un mouvement baissier, etc. On ne demande PAS plusieurs touches pour qu'une zone
    existe : elle est tracee des le premier passage (la confirmation se fait plus tard, au
    retest, via le price action - cf. is_price_action_confirmed). Le bruit est filtre par
    AMPLITUDE du swing (min_swing_pct), pas par nombre de touches - une vraie correction
    n'est par nature vue qu'une seule fois. min_band_pct : largeur minimale garantie pour
    un pivot isole (sinon une zone a 1 seul pivot serait une ligne, pas un rectangle).

    movement_min_swing_pct (demande du boss) : quand plusieurs corrections mineures se
    succedent a l'interieur d'un meme mouvement plus large (des petits creux dans une meme
    tendance haussiere globale, separes par des rebonds qui ne cassent pas vraiment la
    structure), on ne garde que la correction la PLUS BASSE (ou la plus haute pour une
    resistance) de ce mouvement, pas chacune individuellement. On relance build_zigzag une
    2e fois sur le zigzag deja obtenu, avec un seuil plus large - ca absorbe naturellement
    les corrections mineures internes (meme mecanisme que le 1er passage, applique a un
    niveau plus grossier). Si None, pas de 2e passage (comportement actuel inchange)."""
    if len(candles) < left + right + 1:
        return []

    pivots = find_pivots(candles, left=left, right=right)
    if not pivots:
        return []

    zigzag = build_zigzag(pivots, min_swing_pct=min_swing_pct)
    if not zigzag:
        return []

    if movement_min_swing_pct is not None:
        zigzag = build_zigzag(zigzag, min_swing_pct=movement_min_swing_pct)
        if not zigzag:
            return []

    # regroupe encore les points zigzag proches en prix (deux corrections a niveau quasi
    # identique restent une seule zone). IMPORTANT : on compare chaque nouveau pivot au
    # DEBUT du cluster (pas au dernier ajoute), pour eviter un effet de chaine ou une
    # succession de petits ecarts sous tolerance_pct finit par regrouper des pivots tres
    # eloignes les uns des autres (ex: tout un range de prix chahute sur plusieurs mois).
    pivots_sorted = sorted(zigzag, key=lambda p: p.price)
    raw_clusters = []
    current = []
    for p in pivots_sorted:
        if not current:
            current = [p]
            continue
        cluster_min = min(c.price for c in current)
        cluster_max = max(c.price for c in current)
        candidate_min = min(cluster_min, p.price)
        candidate_max = max(cluster_max, p.price)
        if (float(candidate_max) - float(candidate_min)) / float(candidate_min) * 100 <= tolerance_pct:
            current.append(p)
        else:
            raw_clusters.append(current)
            current = [p]
    if current:
        raw_clusters.append(current)

    zones = []
    for cluster in raw_clusters:
        pivot_prices = [c.price for c in cluster]
        if len(cluster) == 1:
            # pivot isole : largeur = ATR local (volatilite typique des bougies VOISINES,
            # pivot exclu - cf. _local_atr) plutot que le high/low de la bougie du pivot
            # elle-meme, qui est structurellement biaisee vers le haut (un pivot est presque
            # toujours une bougie anormalement volatile, puisque c'est souvent cette
            # volatilite qui cree le swing). La zone s'etend dans la direction de la
            # reaction : vers le haut pour un support (low pivot), vers le bas pour une
            # resistance (high pivot) - comme les rectangles du manuel de chartisme.
            p = cluster[0]
            if p.kind == "low":
                low, high = p.price, p.price + p.local_atr
            else:
                low, high = p.price - p.local_atr, p.price
        else:
            # plusieurs pivots regroupes (venant potentiellement de bougies/dates differentes) :
            # la largeur doit refleter l'ecart entre leurs PRIX de pivot (deja borne a
            # tolerance_pct par le clustering), PAS l'union de toutes leurs bougies completes -
            # sinon une bougie eloignee et sans rapport peut faire exploser la largeur meme si
            # les pivots eux-memes sont proches en prix.
            low = min(pivot_prices)
            high = max(pivot_prices)
        if low == high:  # garde-fou improbable (bougie a spread nul) -> padding minimal
            pad = float(low) * (min_band_pct / 100)
            low, high = float(low) - pad, float(high) + pad
        zones.append({
            "price_low": low,
            "price_high": high,
            "touch_count": len(cluster),
            "first_seen": min(c.open_time for c in cluster),
            "last_touched": max(c.open_time for c in cluster),
        })
    return zones


def is_price_action_confirmed(candles):
    """Confirmation price action au retest d'une zone : ralentissement du prix (petit corps
    de bougie), doji, ou bougie de retournement. Prend les 3 dernieres bougies disponibles
    (la plus recente en dernier). Retourne True si un signal de confirmation est detecte."""
    if len(candles) < 2:
        return False

    last = candles[-1]
    body = abs(float(last.close) - float(last.open))
    full_range = float(last.high) - float(last.low)
    if full_range == 0:
        return False

    body_ratio = body / full_range

    # Doji / ralentissement : petit corps par rapport au range de la bougie
    is_doji_or_slowdown = body_ratio <= 0.3

    # Bougie de retournement haussiere : cloture nettement au-dessus de l'ouverture, apres une
    # bougie precedente baissiere (mini logique d'engulfing/pin bar sans etre exhaustif)
    prev = candles[-2]
    prev_bearish = float(prev.close) < float(prev.open)
    curr_bullish = float(last.close) > float(last.open)
    is_bullish_reversal = prev_bearish and curr_bullish and float(last.close) > float(prev.open)

    # Meche basse longue (pin bar / marteau) : rejet net du bas de la bougie
    lower_wick = float(min(last.open, last.close)) - float(last.low)
    is_hammer = full_range > 0 and (lower_wick / full_range) >= 0.5

    return is_doji_or_slowdown or is_bullish_reversal or is_hammer


def build_zigzag(pivots, min_swing_pct=3.0):
    """Reconstitue une sequence zigzag (alternance stricte high/low), en ignorant les
    swings trop faibles (bruit) plutot que de filtrer par nombre de touches - car une
    vraie correction de mouvement n'est par nature touchee qu'une seule fois."""
    pivots_sorted = sorted(pivots, key=lambda p: p.open_time)
    zigzag = []

    for p in pivots_sorted:
        if not zigzag:
            zigzag.append(p)
            continue
        last = zigzag[-1]
        if p.kind == last.kind:
            # deux pivots du meme type d'affilee -> on garde le plus extreme
            if p.kind == "high" and p.price > last.price:
                zigzag[-1] = p
            elif p.kind == "low" and p.price < last.price:
                zigzag[-1] = p
            continue

        amplitude_pct = abs(float(p.price) - float(last.price)) / float(last.price) * 100
        if amplitude_pct < min_swing_pct:
            continue  # swing trop faible -> bruit, on ignore ce pivot

        zigzag.append(p)

    return zigzag

def price_in_zone(price: Decimal, zone: Zone) -> bool:
    return zone.price_low <= price <= zone.price_high


def get_ordered_zones(exchange: str, symbol: str):
    """Toutes les zones actives pour une paire, triées par prix croissant."""
    return list(Zone.objects.filter(exchange=exchange, symbol=symbol, is_active=True).order_by("price_low"))


def find_target_support(zones, current_price: Decimal):
    """Le support 'cible' = la zone la plus proche dont le bas est <= prix courant."""
    candidates = [z for z in zones if z.price_low <= current_price]
    if not candidates:
        return None
    return max(candidates, key=lambda z: z.price_low)


def find_next_zone_above(zones, reference_zone):
    """La zone immédiatement au-dessus d'une zone de référence (TP)."""
    candidates = [z for z in zones if z.price_low > reference_zone.price_high]
    if not candidates:
        return None
    return min(candidates, key=lambda z: z.price_low)


def find_next_zone_below(zones, reference_zone):
    """La zone immédiatement en dessous (nouvelle cible quand un support est cassé)."""
    candidates = [z for z in zones if z.price_high < reference_zone.price_low]
    if not candidates:
        return None
    return max(candidates, key=lambda z: z.price_high)


def build_resistance_ladder(zones, entry_zone):
    """Toutes les zones strictement au-dessus du support d'entrée, triées par prix croissant."""
    candidates = [z for z in zones if z.price_low > entry_zone.price_high]
    return sorted(candidates, key=lambda z: z.price_low)


def compute_tp_percentages(n: int):
    """n <= 4 : 100/n % par palier. n > 4 : 25% au premier, 75% répartis sur les suivants."""
    if n <= 0:
        return []
    if n > 4:
        first = 25.0
        rest = 75.0 / (n - 1)
        return [first] + [rest] * (n - 1)
    return [100.0 / n] * n
