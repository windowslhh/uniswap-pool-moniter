"""Fetch accurate TVL via on-chain balanceOf + slot0 calls on Base using Multicall3.

All calls are batched into a single eth_call via Multicall3,
avoiding rate limits from the public RPC endpoint.
"""
import logging
import math
import struct
import requests
import lp_math
from config import BASE_RPC_URL, PRICE_ORACLE_POOLS, TOKENS

logger = logging.getLogger(__name__)

# Multicall3 on Base (same address on all EVM chains)
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Function selectors
BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")
SLOT0_SELECTOR = bytes.fromhex("3850c7bd")
# aggregate3((address,bool,bytes)[]) selector
AGGREGATE3_SELECTOR = "0x82ad56cb"


def _encode_balance_of_calldata(pool_address):
    """Encode balanceOf(address) calldata."""
    addr_bytes = bytes.fromhex(pool_address.lower().replace("0x", ""))
    return BALANCE_OF_SELECTOR + b'\x00' * 12 + addr_bytes


def _pad_to_32(data):
    """Pad bytes to next multiple of 32."""
    remainder = len(data) % 32
    if remainder == 0:
        return data
    return data + b'\x00' * (32 - remainder)


def _build_multicall_data(calls):
    """Build Multicall3.aggregate3 calldata manually (no web3/eth_abi dependency).

    calls: list of (target_address, calldata_bytes) tuples
    Returns: hex-encoded calldata string for aggregate3
    """
    n = len(calls)
    parts = []

    # Offset to the array data (always 32 for a single dynamic param)
    parts.append(b'\x00' * 31 + b'\x20')  # 0x20 = 32

    # Array length
    parts.append(int.to_bytes(n, 32, 'big'))

    # Pre-compute element data to determine variable offsets
    element_blobs = []
    for target_addr, calldata in calls:
        blob = b''
        # target address (left-padded to 32 bytes)
        addr_bytes = bytes.fromhex(target_addr.lower().replace("0x", ""))
        blob += b'\x00' * 12 + addr_bytes
        # allowFailure = true
        blob += b'\x00' * 31 + b'\x01'
        # offset to callData within this tuple (3 * 32 = 96 = 0x60)
        blob += b'\x00' * 31 + b'\x60'
        # callData length
        blob += int.to_bytes(len(calldata), 32, 'big')
        # callData padded to 32-byte boundary
        blob += _pad_to_32(calldata)
        element_blobs.append(blob)

    # Element offsets (relative to start of array data after length)
    running_offset = n * 32  # after all offset slots
    for blob in element_blobs:
        parts.append(int.to_bytes(running_offset, 32, 'big'))
        running_offset += len(blob)

    # Element data
    for blob in element_blobs:
        parts.append(blob)

    return AGGREGATE3_SELECTOR + b''.join(parts).hex()


def _decode_multicall_result(hex_result, n_calls):
    """Decode aggregate3 return data: (bool success, bytes returnData)[].

    Returns list of (success, uint256_value) tuples.
    """
    data = bytes.fromhex(hex_result.replace("0x", ""))
    results = []

    if len(data) < 64:
        return [(False, 0)] * n_calls

    # Skip array offset (32 bytes) and read array length
    array_offset = int.from_bytes(data[0:32], 'big')
    array_start = array_offset
    array_len = int.from_bytes(data[array_start:array_start + 32], 'big')

    # Read element offsets
    offsets = []
    for i in range(min(array_len, n_calls)):
        off = int.from_bytes(data[array_start + 32 + i * 32:array_start + 64 + i * 32], 'big')
        offsets.append(array_start + 32 + off)  # relative to after array length

    for off in offsets:
        try:
            success = int.from_bytes(data[off:off + 32], 'big') != 0
            # bytes offset within the tuple
            bytes_offset = int.from_bytes(data[off + 32:off + 64], 'big')
            bytes_start = off + bytes_offset
            bytes_len = int.from_bytes(data[bytes_start:bytes_start + 32], 'big')
            if success and bytes_len >= 32:
                value = int.from_bytes(data[bytes_start + 32:bytes_start + 64], 'big')
                results.append((True, value))
            else:
                results.append((False, 0))
        except (IndexError, ValueError):
            results.append((False, 0))

    # Pad if we got fewer results than expected
    while len(results) < n_calls:
        results.append((False, 0))

    return results


def _batch_rpc_call(calls, session=None):
    """Send all balanceOf calls via Multicall3 in a single eth_call.

    calls: list of (token_address, pool_address, decimals, symbol) tuples
    Returns: dict mapping (pool_address, symbol) → human_balance
    """
    s = session or requests.Session()
    balances = {}

    if not calls:
        return balances

    # Build multicall data: (target, calldata) tuples
    mc_calls = [
        (token_addr, _encode_balance_of_calldata(pool_addr))
        for token_addr, pool_addr, _dec, _sym in calls
    ]
    calldata = _build_multicall_data(mc_calls)

    # Single eth_call to Multicall3
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": MULTICALL3_ADDRESS, "data": calldata},
            "latest",
        ],
    }

    try:
        resp = s.post(BASE_RPC_URL, json=rpc_payload, timeout=30)
        resp.raise_for_status()
        rpc_result = resp.json()

        if "error" in rpc_result:
            logger.warning("Multicall3 RPC error: %s", rpc_result["error"])
            return balances

        hex_result = rpc_result.get("result", "0x")
        decoded = _decode_multicall_result(hex_result, len(calls))

        for i, (token_addr, pool_addr, decimals, symbol) in enumerate(calls):
            success, raw_balance = decoded[i]
            if success:
                human_balance = raw_balance / (10 ** decimals)
                balances[(pool_addr.lower(), symbol)] = human_balance
            else:
                logger.debug("balanceOf failed for %s/%s", symbol, pool_addr[:10])

    except Exception as e:
        logger.warning("Multicall3 call failed: %s", e)

    return balances


def fetch_onchain_tvl(pools, token_prices, session=None):
    """Fetch on-chain TVL for a list of V3 pools.

    pools: list of pool config dicts (with address, token0, token1)
    token_prices: dict mapping symbol → USD price
    Returns: dict mapping pool_address → tvl_usd
    """
    # Build all balanceOf calls (V3 only — V4 uses PoolManager singleton)
    calls = []
    for pool in pools:
        addr = pool["address"]
        # Skip V4 pools (bytes32 IDs, not contract addresses)
        if len(addr) > 42:
            continue
        for token_key in ["token0", "token1"]:
            sym = pool[token_key]
            token_info = TOKENS.get(sym)
            if not token_info:
                continue
            calls.append((token_info["address"], addr, token_info["decimals"], sym))

    if not calls:
        return {}

    # Single Multicall3 RPC call for all balances
    all_balances = _batch_rpc_call(calls, session)

    # Compute TVL per pool
    tvl_map = {}
    for pool in pools:
        addr = pool["address"]
        if len(addr) > 42:
            continue
        addr_lower = addr.lower()
        t0_sym = pool["token0"]
        t1_sym = pool["token1"]
        t0_bal = all_balances.get((addr_lower, t0_sym))
        t1_bal = all_balances.get((addr_lower, t1_sym))
        t0_price = token_prices.get(t0_sym, 0)
        t1_price = token_prices.get(t1_sym, 0)

        # Only compute TVL if BOTH token balances were successfully fetched
        if t0_bal is not None and t1_bal is not None and t0_price > 0 and t1_price > 0:
            tvl_map[addr_lower] = round(t0_bal * t0_price + t1_bal * t1_price, 2)

    return tvl_map


def fetch_token_prices_onchain(session=None):
    """Fetch token USD prices by reading slot0() from high-liquidity Uniswap pools.

    Reads sqrtPriceX96 from stablecoin-paired oracle pools via Multicall3,
    then derives USD prices assuming USDC ≈ $1.

    Returns dict mapping symbol → USD price,
    e.g. {"cbBTC": 67379.63, "WETH": 2069.1, "ETH": 2069.1, "USDC": 1.0, "USDT": 1.0}
    """
    s = session or requests.Session()
    prices = {"USDC": 1.0}

    if not PRICE_ORACLE_POOLS:
        return prices

    # Build Multicall3 batch: one slot0() call per oracle pool
    mc_calls = [(oracle["address"], SLOT0_SELECTOR) for oracle in PRICE_ORACLE_POOLS]
    calldata = _build_multicall_data(mc_calls)

    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": MULTICALL3_ADDRESS, "data": calldata},
            "latest",
        ],
    }

    resp = s.post(BASE_RPC_URL, json=rpc_payload, timeout=30)
    resp.raise_for_status()
    rpc_result = resp.json()

    if "error" in rpc_result:
        logger.warning("slot0 Multicall3 RPC error: %s", rpc_result["error"])
        return prices

    hex_result = rpc_result.get("result", "0x")
    decoded = _decode_multicall_result(hex_result, len(PRICE_ORACLE_POOLS))

    for i, oracle in enumerate(PRICE_ORACLE_POOLS):
        success, sqrt_price_x96 = decoded[i]
        if not success or sqrt_price_x96 == 0:
            logger.warning("slot0 failed for oracle pool %s", oracle["address"][:10])
            continue

        # raw_price = token1_raw / token0_raw
        raw_price = lp_math.sqrt_price_x96_to_price(sqrt_price_x96)
        # decimal_price = token1_human / token0_human
        decimal_price = lp_math.raw_price_to_decimal(
            raw_price, oracle["token0_decimals"], oracle["token1_decimals"]
        )

        derive_sym = oracle["derive"]
        t0 = oracle["token0"]
        t1 = oracle["token1"]

        # Derive USD price from the stablecoin-paired price
        if t1 == "USDC" or t1 == "USDT":
            # decimal_price = USDC per token0 → token0 price in USD
            prices[derive_sym] = decimal_price
        elif t0 == "USDC" or t0 == "USDT":
            # decimal_price = token1 per USDC → token1 price = 1/decimal_price
            if decimal_price > 0:
                prices[derive_sym] = 1.0 / decimal_price

    # ETH = WETH alias
    if "WETH" in prices:
        prices["ETH"] = prices["WETH"]
    if "ETH" in prices and "WETH" not in prices:
        prices["WETH"] = prices["ETH"]

    return prices
