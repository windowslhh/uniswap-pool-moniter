import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify, request
from config import (
    DATA_SOURCE,
    GECKO_BASE_URL,
    GECKO_NETWORK,
    POOLS,
    POOLS_V4,
    SUBGRAPH_URL,
    SUBGRAPH_URL_V4,
    THEGRAPH_API_KEY,
    CACHE_TTL,
    FLASK_PORT,
)
import lp_math
import onchain_tvl

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# HTTP session with retry for transient errors (SSL, connection reset)
_session = requests.Session()
_retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 502, 503, 504])
_session.mount("https://", HTTPAdapter(max_retries=_retry))

# Simple in-memory cache
_cache = {"data": None, "timestamp": 0}


# ============================================================
# GeckoTerminal Data Source (Fallback - Free, no API key)
# ============================================================

def gecko_get(path):
    """Make a GET request to GeckoTerminal API."""
    url = f"{GECKO_BASE_URL}{path}"
    headers = {"Accept": "application/json;version=20230302"}
    resp = _session.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def gecko_get_pool_info(pool_address):
    """Get pool basic info from GeckoTerminal."""
    data = gecko_get(f"/networks/{GECKO_NETWORK}/pools/{pool_address}")
    return data.get("data", {})


def gecko_get_ohlcv(pool_address, days=30):
    """Get daily OHLCV data from GeckoTerminal for volume calculation."""
    data = gecko_get(
        f"/networks/{GECKO_NETWORK}/pools/{pool_address}/ohlcv/day"
        f"?aggregate=1&limit={days}&currency=usd"
    )
    return data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])


def fetch_pool_gecko(pool_config):
    """Fetch all data for a single pool using GeckoTerminal."""
    address = pool_config["address"]
    fee_rate = pool_config["fee_tier"] / 100  # e.g., 0.3% -> 0.003

    pool_data = gecko_get_pool_info(address)
    attrs = pool_data.get("attributes", {})

    tvl = float(attrs.get("reserve_in_usd") or 0)
    volume_24h = float(attrs.get("volume_usd", {}).get("h24") or 0)
    base_price_usd = float(attrs.get("base_token_price_usd") or 0)
    quote_price_usd = float(attrs.get("quote_token_price_usd") or 0)

    if quote_price_usd > 0:
        price_ratio = base_price_usd / quote_price_usd
    else:
        price_ratio = 0

    ohlcv = gecko_get_ohlcv(address, days=30)

    volume_1d = volume_24h
    volume_7d = sum(candle[5] for candle in ohlcv[:7]) if len(ohlcv) >= 7 else volume_24h
    volume_30d = sum(candle[5] for candle in ohlcv[:30]) if len(ohlcv) >= 1 else volume_24h

    fees_1d = volume_1d * fee_rate
    fees_7d = volume_7d * fee_rate
    fees_30d = volume_30d * fee_rate

    def calc_apy(fees, days):
        if tvl > 0 and days > 0:
            return (fees / days / tvl) * 365 * 100
        return 0

    return {
        "name": pool_config["name"],
        "address": address,
        "fee_tier": f"{pool_config['fee_tier']}%",
        "token0": pool_config["token0"],
        "token1": pool_config["token1"],
        "price_ratio": price_ratio,
        "base_price_usd": base_price_usd,
        "quote_price_usd": quote_price_usd,
        "tvl_usd": tvl,
        "metrics": {
            "1d": {
                "volume": volume_1d,
                "fees": fees_1d,
                "apy": round(calc_apy(fees_1d, 1), 2),
            },
            "7d": {
                "volume": volume_7d,
                "fees": fees_7d,
                "apy": round(calc_apy(fees_7d, 7), 2),
            },
            "30d": {
                "volume": volume_30d,
                "fees": fees_30d,
                "apy": round(calc_apy(fees_30d, 30), 2),
            },
        },
        "data_source": "GeckoTerminal",
    }


# ============================================================
# TheGraph — Shared Uniswap Official Schema (V3 + V4)
# Both use: pool, poolHourDatas, poolDayDatas
# ============================================================

BATCH_SIZE = 5  # pools per GraphQL request


def _build_batch_query(pool_configs, offset=0):
    """Build a batched GraphQL query for pools using Uniswap official schema.
    Fetches: pool info, hourly snapshots from last 24h, 30 daily snapshots (7d/30d).
    """
    ts_24h_ago = int(time.time()) - 86400
    parts = []
    for i, p in enumerate(pool_configs):
        idx = i + offset
        pid = p["address"]
        parts.append(
            f'pool{idx}: pool(id: "{pid}") '
            f"{{ id feeTier totalValueLockedUSD volumeUSD feesUSD "
            f"tick sqrtPrice liquidity "
            f"token0 {{ symbol decimals }} token1 {{ symbol decimals }} "
            f"token0Price token1Price }}"
        )
        parts.append(
            f'hour{idx}: poolHourDatas('
            f'first: 24, orderBy: periodStartUnix, orderDirection: desc, '
            f'where: {{pool: "{pid}", periodStartUnix_gte: {ts_24h_ago}}}) '
            f"{{ periodStartUnix volumeUSD feesUSD tvlUSD }}"
        )
        parts.append(
            f'day{idx}: poolDayDatas('
            f'first: 30, orderBy: date, orderDirection: desc, '
            f'where: {{pool: "{pid}"}}) '
            f"{{ date volumeUSD feesUSD tvlUSD }}"
        )
    return "{ " + " ".join(parts) + " }"


def _parse_pool_result(pool_data, hour_data, day_data, pool_config, version):
    """Parse pool data from Uniswap official schema (works for both V3 and V4)."""
    if not pool_data:
        raise Exception(f"Pool {pool_config['address'][:20]}... not found")

    tvl = float(pool_data.get("totalValueLockedUSD", 0))
    token0_sym = pool_data.get("token0", {}).get("symbol", pool_config["token0"])
    token1_sym = pool_data.get("token1", {}).get("symbol", pool_config["token1"])

    hours = hour_data or []
    days = day_data or []

    def sum_field(data, field, n):
        return sum(float(d.get(field, 0)) for d in data[:n])

    # Rolling 24h from hourly data (already filtered by periodStartUnix_gte)
    volume_1d = sum(float(h.get("volumeUSD", 0)) for h in hours)
    fees_1d = sum(float(h.get("feesUSD", 0)) for h in hours)

    # 7d and 30d from daily data
    volume_7d = sum_field(days, "volumeUSD", 7)
    volume_30d = sum_field(days, "volumeUSD", 30)
    fees_7d = sum_field(days, "feesUSD", 7)
    fees_30d = sum_field(days, "feesUSD", 30)

    def calc_apy(fees, num_days):
        if tvl > 0 and num_days > 0:
            return (fees / num_days / tvl) * 365 * 100
        return 0

    address = pool_config["address"]
    if version == "V4":
        address = address[:42]

    # Compute decimal-adjusted prices from sqrtPriceX96 (consistent for V3 & V4)
    t0_dec = int(pool_data.get("token0", {}).get("decimals", 18))
    t1_dec = int(pool_data.get("token1", {}).get("decimals", 18))
    sqrt_price = pool_data.get("sqrtPrice") or "0"
    if sqrt_price != "0":
        raw_price = lp_math.sqrt_price_x96_to_price(sqrt_price)
        t0_price_in_t1 = lp_math.raw_price_to_decimal(raw_price, t0_dec, t1_dec)
        t1_price_in_t0 = 1.0 / t0_price_in_t1 if t0_price_in_t1 > 0 else 0
    else:
        t0_price_in_t1 = float(pool_data.get("token0Price", 0))
        t1_price_in_t0 = float(pool_data.get("token1Price", 0))

    return {
        "name": pool_config["name"],
        "address": address,
        "fee_tier": f"{pool_config['fee_tier']}%",
        "token0": token0_sym,
        "token1": token1_sym,
        "price_ratio": float(pool_data.get("token0Price", 0)),
        "base_price_usd": 0,
        "quote_price_usd": 0,
        "tvl_usd": tvl,
        "tick": pool_data.get("tick"),
        "sqrtPrice": pool_data.get("sqrtPrice"),
        "liquidity": pool_data.get("liquidity"),
        "token0_decimals": t0_dec,
        "token1_decimals": t1_dec,
        "token0Price": t0_price_in_t1,
        "token1Price": t1_price_in_t0,
        "metrics": {
            "1d": {
                "volume": volume_1d,
                "fees": fees_1d,
                "apy": round(calc_apy(fees_1d, 1), 2),
            },
            "7d": {
                "volume": volume_7d,
                "fees": fees_7d,
                "apy": round(calc_apy(fees_7d, 7), 2),
            },
            "30d": {
                "volume": volume_30d,
                "fees": fees_30d,
                "apy": round(calc_apy(fees_30d, 30), 2),
            },
        },
        "data_source": f"TheGraph {version}",
        "version": version,
    }


def _fetch_pools_batched(pool_configs, subgraph_url, version):
    """Fetch pools using batched GraphQL queries in parallel."""
    batches = [pool_configs[i:i + BATCH_SIZE] for i in range(0, len(pool_configs), BATCH_SIZE)]
    all_data = {}

    def _fetch_batch(batch, offset):
        query = _build_batch_query(batch, offset)
        payload = {"query": query}
        resp = _session.post(subgraph_url, json=payload, timeout=90)
        resp.raise_for_status()
        result = resp.json()
        if "errors" in result:
            raise Exception(f"{version} Subgraph error: {result['errors']}")
        return result["data"]

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        offset = 0
        for batch in batches:
            futures[executor.submit(_fetch_batch, batch, offset)] = (batch, offset)
            offset += len(batch)

        for future in as_completed(futures):
            data = future.result()
            all_data.update(data)

    results = []
    errors = []
    for i, pc in enumerate(pool_configs):
        try:
            pool_data = all_data.get(f"pool{i}")
            hour_data = all_data.get(f"hour{i}", [])
            day_data = all_data.get(f"day{i}", [])
            results.append(_parse_pool_result(pool_data, hour_data, day_data, pc, version))
        except Exception as e:
            logger.error("Failed to parse %s pool %s: %s", version, pc["name"], e)
            errors.append({"pool": f"{version} {pc['name']}", "error": str(e)})

    return results, errors


# ============================================================
# Unified Data Fetcher
# ============================================================

def fetch_all_pools():
    """Fetch data for all monitored pools (V3 + V4) with caching."""
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    results = []
    errors = []

    if DATA_SOURCE == "thegraph" and THEGRAPH_API_KEY:
        # Fetch V3 and V4 in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            v3_future = executor.submit(_fetch_pools_batched, POOLS, SUBGRAPH_URL, "V3")
            v4_future = executor.submit(_fetch_pools_batched, POOLS_V4, SUBGRAPH_URL_V4, "V4")

            try:
                v3_results, v3_errors = v3_future.result()
                results.extend(v3_results)
                errors.extend(v3_errors)
            except Exception as e:
                logger.warning("V3 TheGraph fetch failed: %s", e)
                errors.append({"pool": "V3 batch", "error": str(e)})

            try:
                v4_results, v4_errors = v4_future.result()
                results.extend(v4_results)
                errors.extend(v4_errors)
            except Exception as e:
                logger.warning("V4 TheGraph fetch failed: %s", e)
                errors.append({"pool": "V4 batch", "error": str(e)})
    else:
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_pool_gecko, pc): pc for pc in POOLS}
            for future in as_completed(futures):
                pc = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    errors.append({"pool": pc["name"], "error": str(e)})

    # Override TVL with accurate on-chain balanceOf data
    if results:
        try:
            token_prices = onchain_tvl.fetch_token_prices_from_subgraph(results)
            all_pools_config = POOLS + POOLS_V4
            tvl_map = onchain_tvl.fetch_onchain_tvl(
                all_pools_config, token_prices, _session
            )
            if tvl_map:
                for pool in results:
                    addr = pool["address"].lower()
                    if addr in tvl_map and tvl_map[addr] > 0:
                        new_tvl = tvl_map[addr]
                        pool["tvl_usd"] = new_tvl
                        # Recalculate APY with corrected TVL
                        for period in ["1d", "7d", "30d"]:
                            m = pool.get("metrics", {}).get(period, {})
                            fees = m.get("fees", 0)
                            days = {"1d": 1, "7d": 7, "30d": 30}[period]
                            if new_tvl > 0 and days > 0:
                                m["apy"] = round(
                                    (fees / days / new_tvl) * 365 * 100, 2
                                )
                logger.info(
                    "On-chain TVL override applied for %d/%d pools",
                    len(tvl_map), len(results),
                )
        except Exception as e:
            logger.warning("On-chain TVL fetch failed, using subgraph TVL: %s", e)

    data = {"pools": results, "errors": errors, "timestamp": int(now)}
    _cache["data"] = data
    _cache["timestamp"] = now
    return data


# ============================================================
# Position Estimate — Concentrated Liquidity Calculator
# ============================================================

def _build_estimate_query(pool_address, hours=168, include_tick=True):
    """Build GraphQL query for position estimation with historical hourly data."""
    hour_fields = '{ periodStartUnix feesUSD volumeUSD tvlUSD }'
    if include_tick:
        hour_fields = '{ periodStartUnix tick liquidity feesUSD volumeUSD tvlUSD }'
    return (
        '{ pool(id: "%s") '
        '{ id feeTier totalValueLockedUSD tick sqrtPrice liquidity '
        'token0 { symbol decimals } token1 { symbol decimals } '
        'token0Price token1Price } '
        'poolHourDatas(first: %d, orderBy: periodStartUnix, orderDirection: desc, '
        'where: {pool: "%s"}) '
        '%s }'
    ) % (pool_address, min(hours, 1000), pool_address, hour_fields)


def _resolve_token_usd_prices(t0_sym, t1_sym, token0_price_in_token1,
                               token1_price_in_token0, current_price, tvl):
    """Determine USD prices for token0 and token1.

    Heuristic: if one token is a stablecoin, its USD price is 1.
    For non-stable pairs (e.g., WETH/cbBTC), estimate from relative prices.
    """
    stables = {"USDC", "USDT", "DAI"}

    if t1_sym in stables:
        return token0_price_in_token1, 1.0
    elif t0_sym in stables:
        return 1.0, token1_price_in_token0
    else:
        # Neither is stable — use relative pricing
        # Assume token0 as base unit = 1, token1 = current_price
        return 1.0, current_price


# ============================================================
# Flask Routes
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/pools")
def api_pools():
    try:
        data = fetch_all_pools()
        return jsonify({"success": True, **data})
    except Exception as e:
        logger.exception("Failed to fetch pool data")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/debug-tvl")
def api_debug_tvl():
    """Debug endpoint to check on-chain TVL status."""
    try:
        from config import BASE_RPC_URL
        test_pool = "0x3e7586d52a9d07f8611b8ecf6ccc8a689c34a659"
        test_token = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # USDC
        padded = test_pool.replace("0x", "").zfill(64)
        rpc_payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": test_token, "data": "0x70a08231" + padded}, "latest"]
        }
        resp = _session.post(BASE_RPC_URL, json=rpc_payload, timeout=15)
        rpc_ok = resp.status_code == 200
        rpc_data = resp.json() if rpc_ok else {}
        usdc_hex = rpc_data.get("result", "0x0")
        usdc_balance = int(usdc_hex, 16) / 1e6 if usdc_hex and usdc_hex != "0x" else 0

        cached = _cache.get("data")
        pool_tvl = None
        if cached:
            for p in cached.get("pools", []):
                if p["address"] == test_pool:
                    pool_tvl = p["tvl_usd"]
                    break

        return jsonify({
            "rpc_url": BASE_RPC_URL,
            "rpc_reachable": rpc_ok,
            "rpc_error": rpc_data.get("error"),
            "test_pool": test_pool,
            "usdc_balance_onchain": usdc_balance,
            "cached_tvl": pool_tvl,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/estimate", methods=["POST"])
def api_estimate():
    """Estimate position-level APY for a concentrated liquidity position."""
    if DATA_SOURCE != "thegraph" or not THEGRAPH_API_KEY:
        return jsonify({
            "success": False,
            "error": "Calculator requires TheGraph data source"
        }), 400

    try:
        body = request.get_json()
        if not body:
            return jsonify({"success": False, "error": "Request body required"}), 400

        pool_address = body.get("pool_address", "").strip().lower()
        price_low = float(body.get("price_low", 0))
        price_high = float(body.get("price_high", 0))
        capital_usd = float(body.get("capital_usd", 0))
        hours = int(body.get("hours", 168))
        inverted = body.get("inverted", False)

        if not pool_address:
            return jsonify({"success": False, "error": "pool_address required"}), 400
        if price_low <= 0 or price_high <= 0:
            return jsonify({"success": False, "error": "Prices must be positive"}), 400
        if price_low >= price_high:
            return jsonify({"success": False, "error": "price_low must be < price_high"}), 400
        if capital_usd <= 0:
            return jsonify({"success": False, "error": "capital must be positive"}), 400

        hours = max(24, min(hours, 720))

        # Find pool config and subgraph URL
        pool_config = None
        subgraph_url = None
        version = None
        for pc in POOLS:
            if pc["address"] == pool_address:
                pool_config, subgraph_url, version = pc, SUBGRAPH_URL, "V3"
                break
        if not pool_config:
            for pc in POOLS_V4:
                if pc["address"] == pool_address or pc["address"][:42] == pool_address:
                    pool_config, subgraph_url, version = pc, SUBGRAPH_URL_V4, "V4"
                    pool_address = pc["address"]
                    break
        if not pool_config:
            return jsonify({"success": False, "error": "Pool not found"}), 404

        # Query TheGraph (try with tick/liquidity fields, fallback without)
        query = _build_estimate_query(pool_address, hours, include_tick=True)
        resp = _session.post(subgraph_url, json={"query": query}, timeout=90)
        resp.raise_for_status()
        result = resp.json()
        if "errors" in result:
            # Retry without tick/liquidity in hourly data (V3 compat)
            query = _build_estimate_query(pool_address, hours, include_tick=False)
            resp = _session.post(subgraph_url, json={"query": query}, timeout=90)
            resp.raise_for_status()
            result = resp.json()
            if "errors" in result:
                return jsonify({"success": False, "error": str(result["errors"])}), 500

        data = result["data"]
        pool_data = data.get("pool")
        hourly_data = data.get("poolHourDatas", [])
        if not pool_data:
            return jsonify({"success": False, "error": "Pool not found in subgraph"}), 404

        # Parse pool state
        current_tick = int(pool_data.get("tick") or 0)
        sqrt_price_x96 = pool_data.get("sqrtPrice") or "0"
        pool_liquidity = int(pool_data.get("liquidity", 0))
        tvl = float(pool_data.get("totalValueLockedUSD", 0))
        t0_sym = pool_data.get("token0", {}).get("symbol", "")
        t1_sym = pool_data.get("token1", {}).get("symbol", "")
        t0_dec = int(pool_data.get("token0", {}).get("decimals", 18))
        t1_dec = int(pool_data.get("token1", {}).get("decimals", 18))

        # Raw price from sqrtPriceX96 (NOT decimal-adjusted)
        current_price_raw = lp_math.sqrt_price_x96_to_price(sqrt_price_x96)
        # Decimal-adjusted prices (human-readable)
        current_t0_in_t1 = lp_math.raw_price_to_decimal(
            current_price_raw, t0_dec, t1_dec
        )
        current_t1_in_t0 = 1.0 / current_t0_in_t1 if current_t0_in_t1 > 0 else 0

        # User prices are decimal-adjusted. Convert to internal (token1/token0)
        # then to raw for tick computation.
        if inverted:
            # User entered token0-per-token1 (e.g., USDC per cbBTC)
            # Internal = token1/token0 = 1/user_price (decimal-adjusted)
            internal_dec_low = 1.0 / price_high if price_high > 0 else 0
            internal_dec_high = 1.0 / price_low if price_low > 0 else 0
        else:
            internal_dec_low = price_low
            internal_dec_high = price_high

        # Convert decimal-adjusted to raw for tick computation
        decimal_to_raw = 10 ** (t1_dec - t0_dec)
        internal_raw_low = internal_dec_low * decimal_to_raw
        internal_raw_high = internal_dec_high * decimal_to_raw

        tick_lower = lp_math.price_to_tick(internal_raw_low)
        tick_upper = lp_math.price_to_tick(internal_raw_high)
        if tick_lower > tick_upper:
            tick_lower, tick_upper = tick_upper, tick_lower

        # Determine token USD prices (per human token, decimal-adjusted)
        token0_usd, token1_usd = _resolve_token_usd_prices(
            t0_sym, t1_sym, current_t0_in_t1, current_t1_in_t0,
            current_t1_in_t0, tvl
        )

        # USD price per RAW unit (to match subgraph liquidity units)
        token0_usd_raw = token0_usd / (10 ** t0_dec)
        token1_usd_raw = token1_usd / (10 ** t1_dec)

        # Calculate user's liquidity in subgraph-compatible raw units
        user_L, amount0_raw, amount1_raw = lp_math.capital_to_liquidity(
            capital_usd, current_price_raw, internal_raw_low, internal_raw_high,
            token0_usd_raw, token1_usd_raw
        )

        # Convert raw amounts to human-readable for display
        amount0 = amount0_raw / (10 ** t0_dec)
        amount1 = amount1_raw / (10 ** t1_dec)
        position_value = amount0 * token0_usd + amount1 * token1_usd

        # Fee share at current state
        is_in_range = tick_lower <= current_tick < tick_upper
        fee_share = user_L / (pool_liquidity + user_L) if pool_liquidity > 0 and is_in_range else 0

        # Check if hourly data has tick/liquidity fields
        has_tick_data = hourly_data and hourly_data[0].get("tick") is not None
        has_liq_data = hourly_data and hourly_data[0].get("liquidity") is not None

        if has_tick_data and has_liq_data:
            # Precise: per-hour fee accumulation with time-in-range
            estimate = lp_math.estimate_position_apy(
                user_L, hourly_data, tick_lower, tick_upper, position_value
            )
        else:
            # Fallback: pool-level fees with current liquidity
            total_fees = sum(float(h.get("feesUSD", 0)) for h in hourly_data)
            total_h = len(hourly_data)
            daily_pool_fees = (total_fees / total_h * 24) if total_h > 0 else 0
            daily_pos_fees = daily_pool_fees * fee_share
            apy = (daily_pos_fees / position_value * 365 * 100) if position_value > 0 else 0
            estimate = {
                "apy": round(apy, 2),
                "time_in_range": None,
                "total_fees": round(total_fees * fee_share, 4),
                "daily_fees": round(daily_pos_fees, 4),
                "weekly_fees": round(daily_pos_fees * 7, 4),
                "hours_analyzed": total_h,
                "hours_in_range": None,
                "fallback": True,
            }

        # IL estimate
        il = None
        if has_tick_data and len(hourly_data) > 1:
            oldest_tick = hourly_data[-1].get("tick")
            if oldest_tick is not None:
                oldest_price_raw = lp_math.tick_to_price(int(oldest_tick))
                il = lp_math.calculate_impermanent_loss(
                    oldest_price_raw, current_price_raw,
                    internal_raw_low, internal_raw_high
                )

        # Display price (decimal-adjusted, user-friendly direction)
        display_price = current_t1_in_t0 if inverted else current_t0_in_t1

        display_addr = pool_config["address"][:42] if version == "V4" else pool_config["address"]

        return jsonify({
            "success": True,
            "pool": {
                "address": display_addr,
                "name": pool_config["name"],
                "fee_tier": f"{pool_config['fee_tier']}%",
                "token0": t0_sym,
                "token1": t1_sym,
                "current_price": display_price,
                "current_tick": current_tick,
                "tvl_usd": tvl,
                "version": version,
            },
            "position": {
                "capital_usd": capital_usd,
                "amount_token0": round(amount0, 8),
                "amount_token1": round(amount1, 8),
                "liquidity": str(int(user_L)) if user_L > 0 else "0",
                "fee_share_pct": round(fee_share * 100, 6),
                "position_value_usd": round(position_value, 2),
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "in_range": is_in_range,
            },
            "estimate": estimate,
            "impermanent_loss": il,
        })

    except (KeyError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.exception("Estimate calculation failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/pool-chart", methods=["POST"])
def api_pool_chart():
    """Return historical APY and fee time-series data for charting."""
    if DATA_SOURCE != "thegraph" or not THEGRAPH_API_KEY:
        return jsonify({"success": False, "error": "Requires TheGraph"}), 400

    try:
        body = request.get_json() or {}
        pool_address = body.get("pool_address", "").strip().lower()
        period = body.get("period", "7d")  # 1d, 7d, 30d
        price_low = float(body.get("price_low", 0))
        price_high = float(body.get("price_high", 0))
        capital_usd = float(body.get("capital_usd", 10000))
        inverted = body.get("inverted", False)

        if not pool_address:
            return jsonify({"success": False, "error": "pool_address required"}), 400

        # Map period to hours and bucket size
        period_map = {
            "1d": (24, 1),       # 24 hourly points
            "7d": (168, 6),      # 28 points (6h buckets)
            "30d": (720, 24),    # 30 points (daily buckets)
        }
        total_hours, bucket_size = period_map.get(period, (168, 6))

        # Find pool config
        pool_config, subgraph_url, version = None, None, None
        for pc in POOLS:
            if pc["address"] == pool_address:
                pool_config, subgraph_url, version = pc, SUBGRAPH_URL, "V3"
                break
        if not pool_config:
            for pc in POOLS_V4:
                if pc["address"] == pool_address or pc["address"][:42] == pool_address:
                    pool_config, subgraph_url, version = pc, SUBGRAPH_URL_V4, "V4"
                    pool_address = pc["address"]
                    break
        if not pool_config:
            return jsonify({"success": False, "error": "Pool not found"}), 404

        # Fetch historical hourly data (try with tick/liquidity, fallback without)
        _pool_fields = (
            '{ tick sqrtPrice liquidity totalValueLockedUSD '
            'token0 { symbol decimals } token1 { symbol decimals } }'
        )
        _hour_fields_full = '{ periodStartUnix tick liquidity feesUSD tvlUSD }'
        _hour_fields_basic = '{ periodStartUnix feesUSD tvlUSD }'

        for hour_fields in [_hour_fields_full, _hour_fields_basic]:
            query = (
                '{ pool(id: "%s") %s '
                'poolHourDatas(first: %d, orderBy: periodStartUnix, '
                'orderDirection: desc, where: {pool: "%s"}) %s }'
            ) % (pool_address, _pool_fields, min(total_hours, 1000),
                 pool_address, hour_fields)
            resp = _session.post(subgraph_url, json={"query": query}, timeout=90)
            resp.raise_for_status()
            result = resp.json()
            if "errors" not in result:
                break
        else:
            return jsonify({"success": False, "error": str(result.get("errors", "Query failed"))}), 500

        pool_data = result["data"].get("pool")
        hourly = result["data"].get("poolHourDatas", [])
        if not pool_data or not hourly:
            return jsonify({"success": False, "error": "No data"}), 404

        t0_dec = int(pool_data.get("token0", {}).get("decimals", 18))
        t1_dec = int(pool_data.get("token1", {}).get("decimals", 18))

        # Compute user tick range if price range given
        has_position = price_low > 0 and price_high > 0 and price_low < price_high
        tick_lower, tick_upper, user_L = 0, 0, 0

        if has_position:
            if inverted:
                idl = 1.0 / price_high if price_high > 0 else 0
                idh = 1.0 / price_low if price_low > 0 else 0
            else:
                idl, idh = price_low, price_high

            decimal_to_raw = 10 ** (t1_dec - t0_dec)
            raw_low = idl * decimal_to_raw
            raw_high = idh * decimal_to_raw

            tick_lower = lp_math.price_to_tick(raw_low)
            tick_upper = lp_math.price_to_tick(raw_high)
            if tick_lower > tick_upper:
                tick_lower, tick_upper = tick_upper, tick_lower

            # Compute user L from capital (for fee share)
            raw_price = lp_math.sqrt_price_x96_to_price(
                pool_data.get("sqrtPrice") or "0"
            )
            t0_in_t1 = lp_math.raw_price_to_decimal(raw_price, t0_dec, t1_dec)
            t1_in_t0 = 1.0 / t0_in_t1 if t0_in_t1 > 0 else 0
            t0_usd, t1_usd = _resolve_token_usd_prices(
                pool_data["token0"]["symbol"], pool_data["token1"]["symbol"],
                t0_in_t1, t1_in_t0, t1_in_t0,
                float(pool_data.get("totalValueLockedUSD", 0))
            )
            t0_usd_raw = t0_usd / (10 ** t0_dec)
            t1_usd_raw = t1_usd / (10 ** t1_dec)
            user_L, _, _ = lp_math.capital_to_liquidity(
                capital_usd, raw_price, raw_low, raw_high,
                t0_usd_raw, t1_usd_raw
            )

        # Reverse to chronological order
        hourly.reverse()

        # Bucket the data
        labels = []
        apy_values = []
        fee_values = []

        for i in range(0, len(hourly), bucket_size):
            bucket = hourly[i:i + bucket_size]
            if not bucket:
                continue

            ts = int(bucket[0].get("periodStartUnix", 0))
            bucket_fees_total = sum(float(h.get("feesUSD", 0)) for h in bucket)
            bucket_tvl = float(bucket[-1].get("tvlUSD", 0)) or float(
                bucket[0].get("tvlUSD", 0)
            )

            # Pool-level APY for this bucket
            hours_in_bucket = len(bucket)
            if bucket_tvl > 0 and hours_in_bucket > 0:
                daily_rate = (bucket_fees_total / hours_in_bucket * 24) / bucket_tvl
                pool_apy = daily_rate * 365 * 100
            else:
                pool_apy = 0

            # Position-level fee for this bucket
            pos_fee = 0
            if has_position and user_L > 0:
                for h in bucket:
                    h_tick = h.get("tick")
                    h_liq = h.get("liquidity")
                    h_fee = float(h.get("feesUSD", 0))
                    if h_tick is not None and h_liq is not None:
                        if tick_lower <= int(h_tick) < tick_upper:
                            liq = int(h_liq)
                            if liq > 0:
                                pos_fee += h_fee * user_L / (liq + user_L)

            # Position APY
            if has_position and capital_usd > 0 and hours_in_bucket > 0:
                pos_daily = (pos_fee / hours_in_bucket) * 24
                pos_apy = (pos_daily / capital_usd) * 365 * 100
            else:
                pos_apy = pool_apy

            labels.append(ts)
            apy_values.append(round(pos_apy if has_position else pool_apy, 2))
            fee_values.append(round(pos_fee if has_position else bucket_fees_total, 4))

        return jsonify({
            "success": True,
            "period": period,
            "labels": labels,
            "apy": apy_values,
            "fees": fee_values,
            "has_position": has_position,
        })

    except Exception as e:
        logger.exception("Chart data failed")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    logger.info("Starting Uniswap Pool Monitor on port %d", FLASK_PORT)
    logger.info("Primary data source: %s", DATA_SOURCE)
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True)
