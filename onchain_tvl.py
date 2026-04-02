"""Fetch accurate TVL via on-chain balanceOf calls on Base."""
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import BASE_RPC_URL, TOKENS

logger = logging.getLogger(__name__)

# ERC-20 balanceOf(address) selector
BALANCE_OF_SELECTOR = "0x70a08231"


def _encode_balance_call(token_address, pool_address):
    """Encode an eth_call for balanceOf(pool_address)."""
    # balanceOf(address) → pad address to 32 bytes
    padded = pool_address.lower().replace("0x", "").zfill(64)
    return {
        "to": token_address,
        "data": BALANCE_OF_SELECTOR + padded,
    }


def _batch_rpc_call(calls, session=None):
    """Send a batch JSON-RPC eth_call request to Base RPC.

    calls: list of (token_address, pool_address, decimals, symbol) tuples
    Returns: dict mapping (pool_address, symbol) → human_balance
    """
    s = session or requests.Session()
    batch = []
    for i, (token_addr, pool_addr, _dec, _sym) in enumerate(calls):
        batch.append({
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_call",
            "params": [
                _encode_balance_call(token_addr, pool_addr),
                "latest",
            ],
        })

    try:
        resp = s.post(BASE_RPC_URL, json=batch, timeout=30)
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        logger.error("RPC batch call failed: %s", e)
        return {}

    balances = {}
    # Sort results by id to match calls
    if isinstance(results, list):
        results.sort(key=lambda r: r.get("id", 0))

    for i, (token_addr, pool_addr, decimals, symbol) in enumerate(calls):
        try:
            if isinstance(results, list) and i < len(results):
                r = results[i]
            else:
                continue
            hex_val = r.get("result", "0x0")
            raw_balance = int(hex_val, 16) if hex_val else 0
            human_balance = raw_balance / (10 ** decimals)
            balances[(pool_addr.lower(), symbol)] = human_balance
        except (ValueError, TypeError) as e:
            logger.warning("Failed to parse balance for %s/%s: %s", symbol, pool_addr[:10], e)

    return balances


def fetch_onchain_tvl(pools, token_prices, session=None):
    """Fetch on-chain TVL for a list of pools.

    pools: list of pool config dicts (with address, token0, token1)
    token_prices: dict mapping symbol → USD price (e.g., {"WETH": 2500, "cbBTC": 84000, "USDC": 1, "USDT": 1})
    Returns: dict mapping pool_address → tvl_usd
    """
    # Build all balanceOf calls
    calls = []
    for pool in pools:
        addr = pool["address"][:42]  # Handle V4 long addresses
        for token_key in ["token0", "token1"]:
            sym = pool[token_key]
            token_info = TOKENS.get(sym)
            if not token_info:
                continue
            calls.append((token_info["address"], addr, token_info["decimals"], sym))

    if not calls:
        return {}

    # Batch in groups of 50 (RPC limit)
    all_balances = {}
    batch_size = 50
    for i in range(0, len(calls), batch_size):
        batch = calls[i:i + batch_size]
        balances = _batch_rpc_call(batch, session)
        all_balances.update(balances)

    # Compute TVL per pool
    tvl_map = {}
    for pool in pools:
        addr = pool["address"][:42].lower()
        tvl = 0
        for token_key in ["token0", "token1"]:
            sym = pool[token_key]
            balance = all_balances.get((addr, sym), 0)
            price = token_prices.get(sym, 0)
            tvl += balance * price
        tvl_map[addr] = round(tvl, 2)

    return tvl_map


def fetch_token_prices_from_subgraph(pool_results):
    """Extract token USD prices from already-fetched pool data.

    Uses stablecoin-paired pools to determine token prices.
    pool_results: list of parsed pool result dicts from _parse_pool_result
    Returns: dict mapping symbol → USD price
    """
    prices = {"USDC": 1.0, "USDT": 1.0, "DAI": 1.0}
    stables = set(prices.keys())

    for pool in pool_results:
        t0 = pool.get("token0", "")
        t1 = pool.get("token1", "")
        t0_price = pool.get("token0Price", 0)  # t1 per t0
        t1_price = pool.get("token1Price", 0)  # t0 per t1
        tvl = pool.get("tvl_usd", 0)

        # Use the pool with highest TVL for each token price
        if t1 in stables and t0 not in stables and t0_price > 0:
            # t0_price = how many stablecoins per token0
            if t0 not in prices or tvl > prices.get(f"_tvl_{t0}", 0):
                prices[t0] = t0_price
                prices[f"_tvl_{t0}"] = tvl
        elif t0 in stables and t1 not in stables and t1_price > 0:
            # t1_price = how many stablecoins per token1
            if t1 not in prices or tvl > prices.get(f"_tvl_{t1}", 0):
                prices[t1] = t1_price
                prices[f"_tvl_{t1}"] = tvl

    # Derive ETH = WETH
    if "WETH" in prices and "ETH" not in prices:
        prices["ETH"] = prices["WETH"]
    elif "ETH" in prices and "WETH" not in prices:
        prices["WETH"] = prices["ETH"]

    # Clean up internal keys
    return {k: v for k, v in prices.items() if not k.startswith("_tvl_")}
