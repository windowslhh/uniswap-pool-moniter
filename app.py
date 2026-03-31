import time
import logging
import requests
from flask import Flask, render_template, jsonify
from config import (
    DATA_SOURCE,
    GECKO_BASE_URL,
    GECKO_NETWORK,
    POOLS,
    SUBGRAPH_URL,
    THEGRAPH_API_KEY,
    CACHE_TTL,
    FLASK_PORT,
)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Simple in-memory cache
_cache = {"data": None, "timestamp": 0}


# ============================================================
# GeckoTerminal Data Source (Primary - Free, no API key)
# ============================================================

def gecko_get(path):
    """Make a GET request to GeckoTerminal API."""
    url = f"{GECKO_BASE_URL}{path}"
    headers = {"Accept": "application/json;version=20230302"}
    resp = requests.get(url, headers=headers, timeout=30)
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

    # Get pool info (TVL, 24h volume, price)
    pool_data = gecko_get_pool_info(address)
    attrs = pool_data.get("attributes", {})

    tvl = float(attrs.get("reserve_in_usd") or 0)
    volume_24h = float(attrs.get("volume_usd", {}).get("h24") or 0)
    base_price_usd = float(attrs.get("base_token_price_usd") or 0)
    quote_price_usd = float(attrs.get("quote_token_price_usd") or 0)

    # Price ratio: base/quote
    if quote_price_usd > 0:
        price_ratio = base_price_usd / quote_price_usd
    else:
        price_ratio = 0

    # Get daily OHLCV for 30 days to calculate 7d and 30d volume
    ohlcv = gecko_get_ohlcv(address, days=30)
    # OHLCV format: [timestamp, open, high, low, close, volume]

    volume_1d = volume_24h
    volume_7d = sum(candle[5] for candle in ohlcv[:7]) if len(ohlcv) >= 7 else volume_24h
    volume_30d = sum(candle[5] for candle in ohlcv[:30]) if len(ohlcv) >= 1 else volume_24h

    # Fees = volume * fee_rate
    fees_1d = volume_1d * fee_rate
    fees_7d = volume_7d * fee_rate
    fees_30d = volume_30d * fee_rate

    # APY = (avg_daily_fees / TVL) * 365 * 100
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
# TheGraph Data Source (Fallback - requires API key)
# ============================================================

def query_subgraph(query, variables=None):
    """Send GraphQL POST to TheGraph subgraph."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(SUBGRAPH_URL, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if "errors" in result:
        raise Exception(f"Subgraph error: {result['errors']}")
    return result["data"]


def fetch_pool_thegraph(pool_config):
    """Fetch all data for a single pool using TheGraph subgraph."""
    pool_id = pool_config["address"]
    fee_rate = pool_config["fee_tier"] / 100

    # Query pool info
    pool_query = """
    query($poolId: String!) {
      pool(id: $poolId) {
        id
        token0 { symbol decimals }
        token1 { symbol decimals }
        feeTier
        liquidity
        totalValueLockedUSD
        token0Price
        token1Price
        volumeUSD
      }
    }
    """
    pool_data = query_subgraph(pool_query, {"poolId": pool_id})
    pool = pool_data.get("pool")
    if not pool:
        raise Exception(f"Pool {pool_id} not found in subgraph")

    tvl = float(pool.get("totalValueLockedUSD", 0))

    # Query poolDayDatas
    day_query = """
    query($poolId: String!) {
      poolDayDatas(
        first: 30,
        orderBy: date,
        orderDirection: desc,
        where: { pool: $poolId }
      ) {
        date
        volumeUSD
        feesUSD
        tvlUSD
      }
    }
    """
    day_data = query_subgraph(day_query, {"poolId": pool_id})
    days = day_data.get("poolDayDatas", [])

    def sum_field(data, field, n):
        return sum(float(d[field]) for d in data[:n])

    volume_1d = sum_field(days, "volumeUSD", 1)
    volume_7d = sum_field(days, "volumeUSD", 7)
    volume_30d = sum_field(days, "volumeUSD", 30)

    # Subgraph provides feesUSD directly
    fees_1d = sum_field(days, "feesUSD", 1)
    fees_7d = sum_field(days, "feesUSD", 7)
    fees_30d = sum_field(days, "feesUSD", 30)

    def calc_apy(fees, num_days):
        if tvl > 0 and num_days > 0:
            return (fees / num_days / tvl) * 365 * 100
        return 0

    return {
        "name": pool_config["name"],
        "address": pool_id,
        "fee_tier": f"{pool_config['fee_tier']}%",
        "token0": pool_config["token0"],
        "token1": pool_config["token1"],
        "price_ratio": float(pool.get("token0Price", 0)),
        "base_price_usd": 0,
        "quote_price_usd": 0,
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
        "data_source": "TheGraph",
    }


# ============================================================
# Unified Data Fetcher
# ============================================================

def fetch_all_pools():
    """Fetch data for all monitored pools with caching."""
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    results = []
    errors = []

    for pool_config in POOLS:
        try:
            # Try primary data source first
            if DATA_SOURCE == "thegraph" and THEGRAPH_API_KEY:
                info = fetch_pool_thegraph(pool_config)
            else:
                info = fetch_pool_gecko(pool_config)
            results.append(info)
        except Exception as e:
            logger.warning(
                "Primary source failed for %s: %s, trying fallback...",
                pool_config["name"], e,
            )
            # Try fallback
            try:
                if DATA_SOURCE == "thegraph" or not THEGRAPH_API_KEY:
                    info = fetch_pool_gecko(pool_config)
                else:
                    info = fetch_pool_thegraph(pool_config)
                results.append(info)
            except Exception as e2:
                logger.error("Both sources failed for %s: %s", pool_config["name"], e2)
                errors.append({"pool": pool_config["name"], "error": str(e2)})

    data = {"pools": results, "errors": errors, "timestamp": int(now)}
    _cache["data"] = data
    _cache["timestamp"] = now
    return data


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


if __name__ == "__main__":
    logger.info("Starting Uniswap Pool Monitor on port %d", FLASK_PORT)
    logger.info("Primary data source: %s", DATA_SOURCE)
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True)
