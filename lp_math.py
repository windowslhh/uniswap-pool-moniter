"""Uniswap V3/V4 concentrated liquidity math utilities."""
import math


# Uniswap V3 tick boundaries
MIN_TICK = -887272
MAX_TICK = 887272


def price_to_tick(price):
    """Convert price (token1/token0) to tick index."""
    if price <= 0:
        return MIN_TICK
    return math.floor(math.log(price) / math.log(1.0001))


def tick_to_price(tick):
    """Convert tick index to price (token1/token0)."""
    return 1.0001 ** tick


def tick_to_sqrt_price(tick):
    """Convert tick to sqrt(price)."""
    return 1.0001 ** (tick / 2)


def sqrt_price_x96_to_price(sqrt_price_x96):
    """Convert sqrtPriceX96 (Q64.96 format) to raw price (token1_raw/token0_raw).
    This is NOT decimal-adjusted."""
    return (int(sqrt_price_x96) / (2 ** 96)) ** 2


def raw_price_to_decimal(raw_price, token0_decimals, token1_decimals):
    """Convert raw price to decimal-adjusted price (token1_human / token0_human)."""
    return raw_price * (10 ** (token0_decimals - token1_decimals))


def sqrt_price_x96_to_sqrt(sqrt_price_x96):
    """Convert sqrtPriceX96 to sqrt(price) as a float."""
    return int(sqrt_price_x96) / (2 ** 96)


def get_amounts_for_liquidity(L, sp, sa, sb):
    """Calculate token amounts from liquidity and sqrt price bounds.

    L: liquidity value
    sp: current sqrt(price)
    sa: sqrt(lower price bound)
    sb: sqrt(upper price bound)
    Returns (amount0, amount1).
    """
    sp_clamped = max(min(sp, sb), sa)
    amount0 = L * (sb - sp_clamped) / (sp_clamped * sb) if sp_clamped > 0 and sb > 0 else 0
    amount1 = L * (sp_clamped - sa) if sp_clamped > sa else 0
    return amount0, amount1


def capital_to_liquidity(capital_usd, current_price, price_low, price_high,
                         token0_price_usd, token1_price_usd):
    """Convert USD capital + price range to liquidity value.

    current_price, price_low, price_high: in token1/token0 units (subgraph native).
    token0_price_usd, token1_price_usd: USD price of each token.
    Returns (liquidity, amount_token0, amount_token1).
    """
    sp = math.sqrt(current_price)
    sa = math.sqrt(price_low)
    sb = math.sqrt(price_high)

    if sa >= sb or sa <= 0:
        return 0, 0, 0

    if sp <= sa:
        # Below range: position is 100% token0
        amount0_per_L = (sb - sa) / (sa * sb)
        usd_per_L = amount0_per_L * token0_price_usd
    elif sp < sb:
        # In range: mix of both tokens
        amount0_per_L = (sb - sp) / (sp * sb)
        amount1_per_L = sp - sa
        usd_per_L = amount0_per_L * token0_price_usd + amount1_per_L * token1_price_usd
    else:
        # Above range: position is 100% token1
        amount1_per_L = sb - sa
        usd_per_L = amount1_per_L * token1_price_usd

    if usd_per_L <= 0:
        return 0, 0, 0

    L = capital_usd / usd_per_L
    amount0, amount1 = get_amounts_for_liquidity(L, sp, sa, sb)
    return L, amount0, amount1


def estimate_position_apy(user_L, hourly_snapshots, tick_lower, tick_upper,
                          position_value_usd):
    """Estimate APY using historical hourly data with time-in-range analysis.

    user_L: user's liquidity value
    hourly_snapshots: list of dicts with keys: tick, liquidity, feesUSD
    tick_lower, tick_upper: user's position tick range
    position_value_usd: USD value of the position

    Returns dict with apy, time_in_range, total_fees, daily_fees, etc.
    """
    if not hourly_snapshots or position_value_usd <= 0 or user_L <= 0:
        return {
            "apy": 0, "time_in_range": 0, "total_fees": 0,
            "daily_fees": 0, "weekly_fees": 0,
            "hours_analyzed": 0, "hours_in_range": 0,
        }

    total_hours = len(hourly_snapshots)
    hours_in_range = 0
    total_fees_earned = 0.0

    for snap in hourly_snapshots:
        snap_tick = snap.get("tick")
        snap_liquidity = snap.get("liquidity")
        snap_fees = float(snap.get("feesUSD", 0))

        if snap_tick is None or snap_liquidity is None:
            continue

        snap_tick = int(snap_tick)
        snap_liquidity = int(snap_liquidity)

        if tick_lower <= snap_tick < tick_upper:
            hours_in_range += 1
            if snap_liquidity > 0:
                # Account for user's own liquidity diluting the pool
                fee_share = user_L / (snap_liquidity + user_L)
                total_fees_earned += snap_fees * fee_share

    time_in_range = hours_in_range / total_hours if total_hours > 0 else 0
    daily_fees = (total_fees_earned / total_hours) * 24 if total_hours > 0 else 0
    weekly_fees = daily_fees * 7
    apy = (daily_fees / position_value_usd) * 365 * 100 if position_value_usd > 0 else 0

    return {
        "apy": round(apy, 2),
        "time_in_range": round(time_in_range * 100, 1),
        "total_fees": round(total_fees_earned, 4),
        "daily_fees": round(daily_fees, 4),
        "weekly_fees": round(weekly_fees, 4),
        "hours_analyzed": total_hours,
        "hours_in_range": hours_in_range,
    }


def calculate_impermanent_loss(price_initial, price_final, price_low, price_high):
    """Calculate IL for a concentrated liquidity position.

    All prices in token1/token0 units.
    Returns IL as a percentage (e.g., -5.0 means 5% loss vs holding).
    """
    sa = math.sqrt(price_low)
    sb = math.sqrt(price_high)

    if sa >= sb or sa <= 0:
        return 0

    sp_i = max(min(math.sqrt(price_initial), sb), sa)
    sp_f = max(min(math.sqrt(price_final), sb), sa)

    # Amounts per unit L at initial price
    a0_i = (sb - sp_i) / (sp_i * sb) if sp_i > 0 and sb > 0 else 0
    a1_i = sp_i - sa if sp_i > sa else 0

    # Value if held initial amounts at final price
    hold_value = a0_i * price_final + a1_i

    # LP value at final price (amounts change with price)
    a0_f = (sb - sp_f) / (sp_f * sb) if sp_f > 0 and sb > 0 else 0
    a1_f = sp_f - sa if sp_f > sa else 0
    lp_value = a0_f * price_final + a1_f

    if hold_value <= 0:
        return 0

    return round((lp_value - hold_value) / hold_value * 100, 2)
