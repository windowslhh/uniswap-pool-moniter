import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify
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
    Fetches: pool info, 24 hourly snapshots (rolling 24h), 30 daily snapshots (7d/30d).
    """
    parts = []
    for i, p in enumerate(pool_configs):
        idx = i + offset
        pid = p["address"]
        parts.append(
            f'pool{idx}: pool(id: "{pid}") '
            f"{{ id feeTier totalValueLockedUSD volumeUSD feesUSD "
            f"token0 {{ symbol }} token1 {{ symbol }} "
            f"token0Price token1Price }}"
        )
        parts.append(
            f'hour{idx}: poolHourDatas('
            f'first: 24, orderBy: periodStartUnix, orderDirection: desc, '
            f'where: {{pool: "{pid}"}}) '
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

    # Rolling 24h from hourly data
    volume_1d = sum_field(hours, "volumeUSD", 24)
    fees_1d = sum_field(hours, "feesUSD", 24)

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
