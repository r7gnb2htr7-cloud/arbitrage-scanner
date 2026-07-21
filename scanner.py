#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сканер межбиржевого спотового арбитража MEXC <-> Bybit.
ТОЛЬКО сканирование, вывод в консоль и уведомления в Telegram.
Никакой торговли, никаких ордеров.

Зависимости: aiohttp
    pip install aiohttp

API-ключи (опционально, только READ) из переменных окружения:
    MEXC_API_KEY, MEXC_API_SECRET, BYBIT_API_KEY, BYBIT_API_SECRET
Ключ Bybit должен иметь право Account/Wallet -> Read, иначе
/v5/asset/coin/query-info вернёт ошибку доступа и сканер уйдёт в fallback.
Без ключей — fallback-режим с явным предупреждением.

Telegram-уведомления (опционально) из переменных окружения:
    TG_BOT_TOKEN — токен бота от @BotFather
    TG_CHAT_ID   — ID личного чата или канала (для канала — с минусом;
                   бот должен быть админом канала с правом публикации)
Без этих переменных сканер работает как обычно, просто молчит в Telegram.

Известные ограничения (не устранимы на стороне скрипта):
- Если хотя бы одна биржа не отдаёт адрес контракта, сверить, что тикер
  означает один и тот же токен, невозможно (помечается в примечании).
- Флаги deposit/withdraw в API бирж могут обновляться с задержкой:
  сеть может быть закрыта на обслуживание, а по API числиться доступной.
- Оценка по мгновенному стакану: за время перевода монеты цена уйдёт.
"""

import asyncio
import hashlib
import hmac
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import aiohttp

# ============================ КОНФИГ ============================

CONFIG = {
    "POSITION_USDT": 2000.0,

    # Минимальный СЫРОЙ спред по тикерам (%) для попадания в кандидаты.
    "MIN_GROSS_SPREAD_PCT": 0.5,
    # Минимальный ЧИСТЫЙ спред для вывода в отчёт (%).
    "MIN_NET_SPREAD_PCT": 0.4,
    # Максимальный валовый спред (%). Выше — почти наверняка аномалия.
    "MAX_GROSS_SPREAD_PCT": 8.0,

    "MIN_24H_QUOTE_VOLUME": 300_000.0,

    "TAKER_FEE_MEXC": 0.0005,   # 0.05%
    "TAKER_FEE_BYBIT": 0.0010,  # 0.10%

    "MEXC_DEPTH_LIMIT": 200,
    "BYBIT_DEPTH_LIMIT": 200,   # v5 spot orderbook: [1, 200]

    # У MEXC жёсткие лимиты на /api/v3/depth: при 429 сработают ретраи,
    # но итерация растянется. При частых ретраях снижайте конкурентность.
    "MAX_CONCURRENCY": 8,
    "REQUEST_PAUSE": 0.08,
    "MAX_RETRIES": 3,           # ретраи на 429/5xx/сетевые ошибки

    # Максимум кандидатов, по которым тянем стаканы (топ по сырому спреду).
    "MAX_CANDIDATES": 120,

    # Используется ТОЛЬКО без ключей. Для ERC20 реальная комиссия часто выше.
    "FALLBACK_WITHDRAW_FEE_USDT": 1.0,

    "LOOP": True,
    "LOOP_INTERVAL_SEC": 30,
    # В LOOP-режиме данные о сетях перезагружаются с этим интервалом.
    "NETWORKS_REFRESH_SEC": 1800,

    "HTTP_TIMEOUT": 15,         # общий таймаут запроса
    "CONNECT_TIMEOUT": 5,       # отдельный таймаут на установку соединения
}

# Ключи ТОЛЬКО из окружения. Не хардкодить.
KEYS = {
    "MEXC_API_KEY": os.getenv("MEXC_API_KEY", ""),
    "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET", ""),
    "BYBIT_API_KEY": os.getenv("BYBIT_API_KEY", ""),
    "BYBIT_API_SECRET": os.getenv("BYBIT_API_SECRET", ""),
}

MEXC_BASE = "https://api.mexc.com"
BYBIT_BASE = "https://api.bybit.com"

# ==================== НОРМАЛИЗАЦИЯ СЕТЕЙ ====================
# Точное совпадение проверяется до подстрочного, поэтому явные записи
# (Arbitrum Nova, Polygon zkEVM — это ОТДЕЛЬНЫЕ сети) побеждают общие алиасы.

NETWORK_ALIASES = {
    "trc20": "TRON", "tron": "TRON", "trx": "TRON",
    "bep20": "BSC", "bsc": "BSC", "bnb smart chain": "BSC",
    "binance smart chain": "BSC", "bep20bsc": "BSC",
    "erc20": "ETH", "eth": "ETH", "ethereum": "ETH",
    "matic": "MATIC", "polygon": "MATIC", "polygon pos": "MATIC",
    "polygonpos": "MATIC", "pol": "MATIC",
    "polygon zkevm": "POLYGONZKEVM", "polygonzkevm": "POLYGONZKEVM",
    "sol": "SOL", "solana": "SOL",
    "arb": "ARB", "arbi": "ARB", "arbitrum": "ARB", "arbitrum one": "ARB",
    "arbitrumone": "ARB", "arbevm": "ARB",
    "arbitrum nova": "ARBNOVA", "arbitrumnova": "ARBNOVA", "arbnova": "ARBNOVA",
    "op": "OP", "optimism": "OP", "opeth": "OP", "opmainnet": "OP",
    "avax": "AVAX", "avaxc": "AVAX", "avax_c": "AVAX", "avalanche": "AVAX",
    "avaxcchain": "AVAX", "cchain": "AVAX",
    "base": "BASE", "baseevm": "BASE", "basemainnet": "BASE",
    "ton": "TON", "the open network": "TON", "toncoin": "TON",
    "apt": "APT", "aptos": "APT",
    "sui": "SUI",
    "ada": "ADA", "cardano": "ADA",
    "dot": "DOT", "polkadot": "DOT",
    "ltc": "LTC", "litecoin": "LTC",
    "xrp": "XRP", "ripple": "XRP",
    "btc": "BTC", "bitcoin": "BTC",
    "zksync": "ZKSYNC", "zksync era": "ZKSYNC", "zksera": "ZKSYNC",
    "zksyncera": "ZKSYNC",
    "near": "NEAR",
    "celo": "CELO",
    "mantle": "MANTLE", "mnt": "MANTLE",
    "linea": "LINEA",
    "opbnb": "OPBNB",
    "scroll": "SCROLL",
    "blast": "BLAST",
    "sei": "SEI", "seievm": "SEI",
    "kava": "KAVA", "kavaevm": "KAVA",
    "ftm": "FTM", "fantom": "FTM",
    "cro": "CRO", "cronos": "CRO",
    "atom": "ATOM", "cosmos": "ATOM",
    "algo": "ALGO", "algorand": "ALGO",
    "xlm": "XLM", "stellar": "XLM",
    "doge": "DOGE", "dogecoin": "DOGE",
    "bch": "BCH", "bitcoin cash": "BCH",
    "etc": "ETC", "ethereum classic": "ETC",
    "strk": "STARKNET", "starknet": "STARKNET",
    "inj": "INJ", "injective": "INJ",
    "hbar": "HBAR", "hedera": "HBAR",
    "vet": "VET", "vechain": "VET",
    "egld": "EGLD", "elrond": "EGLD", "multiversx": "EGLD",
    "icp": "ICP", "internet computer": "ICP",
    "fil": "FIL", "filecoin": "FIL",
    "flow": "FLOW",
    "klay": "KLAY", "klaytn": "KLAY", "kaia": "KLAY",
    "osmo": "OSMO", "osmosis": "OSMO",
    "xtz": "XTZ", "tezos": "XTZ",
    "waves": "WAVES",
    "one": "ONE", "harmony": "ONE",
    "rune": "RUNE", "thorchain": "RUNE",
}

# Для подстрочного поиска: длинные (специфичные) алиасы проверяются первыми,
# и только длиной >= 4, чтобы 'op'/'arb'/'eth' не давали ложных срабатываний.
_SUBSTR_ALIASES = sorted(
    ((a, c) for a, c in NETWORK_ALIASES.items() if len(a) >= 4),
    key=lambda x: len(x[0]), reverse=True,
)


def normalize_network(raw: str) -> str:
    """Канонизация имени сети. Точное совпадение -> очищенное ->
    подстрока (от длинных алиасов к коротким, только длиной >= 4)."""
    if not raw:
        return ""
    s = raw.strip().lower()
    if s in NETWORK_ALIASES:
        return NETWORK_ALIASES[s]
    cleaned = "".join(ch for ch in s if ch.isalnum())
    if cleaned in NETWORK_ALIASES:
        return NETWORK_ALIASES[cleaned]
    for alias, canon in _SUBSTR_ALIASES:
        if alias in s or alias in cleaned:  # например 'bep20(bsc)'
            return canon
    return raw.strip().upper()


# ============================ МОДЕЛИ ============================

@dataclass
class NetworkInfo:
    canon: str
    raw_name: str
    deposit: bool
    withdraw: bool
    withdraw_fee_coin: float
    withdraw_min_coin: float = 0.0
    deposit_min_coin: float = 0.0
    contract: str = ""


@dataclass
class CoinNetworks:
    coin: str
    networks: "dict[str, NetworkInfo]" = field(default_factory=dict)


@dataclass
class SymbolFilters:
    """Торговые ограничения пары. 0.0 = биржа не сообщила значение."""
    min_qty: float = 0.0        # минимальный размер ордера в базовой монете
    min_notional: float = 0.0   # минимальная сумма ордера в USDT
    qty_step: float = 0.0       # шаг количества (базовая монета)


@dataclass
class NetChoice:
    canon: str
    fee_coin: float
    fee_usdt: float
    verified: bool          # данные бирж реально проверены (ключи есть)
    contract_checked: bool  # контракты сверены на обеих биржах


@dataclass
class Opportunity:
    coin: str
    direction: str
    network: str
    net_spread_pct: float
    gross_spread_pct: float
    net_profit_usdt: float
    spent_usdt: float
    buy_vwap: float
    sell_vwap: float
    wd_fee_usdt: float
    depth_ok: bool
    note: str = ""


def _f(x, default=0.0) -> float:
    """Безопасный float для полей API."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def floor_step(qty: float, step: float) -> float:
    """Округление количества вниз к шагу биржи."""
    if step and step > 0:
        return math.floor(qty / step) * step
    return qty


# ============================ HTTP ============================

class Http:
    def __init__(self, session: aiohttp.ClientSession, sem: asyncio.Semaphore):
        self.session = session
        self.sem = sem
        self.retried = 0  # сколько раз пришлось ретраить (индикатор rate-limit)

    async def get_json(self, url: str, params=None, headers=None):
        """GET с ретраями и джиттером на 429/5xx и сетевых ошибках."""
        last_err = None
        async with self.sem:
            for attempt in range(CONFIG["MAX_RETRIES"] + 1):
                if attempt > 0:
                    self.retried += 1
                    # Экспоненциальный бэкофф + джиттер, чтобы параллельные
                    # задачи не били в лимит синхронно.
                    await asyncio.sleep(
                        0.5 * (2 ** (attempt - 1)) + random.uniform(0.0, 0.3)
                    )
                await asyncio.sleep(CONFIG["REQUEST_PAUSE"])
                try:
                    async with self.session.get(
                        url, params=params, headers=headers
                    ) as r:
                        txt = await r.text()
                        if r.status == 429 or r.status >= 500:
                            last_err = f"HTTP {r.status}: {txt[:200]}"
                            continue  # ретрай
                        if r.status != 200:
                            return None, f"HTTP {r.status}: {txt[:200]}"
                        return json.loads(txt), None
                except Exception as e:
                    last_err = f"EXC {type(e).__name__}: {e}"
                    continue  # ретрай
        return None, last_err


# ============================ MEXC ============================

class Mexc:
    def __init__(self, http: Http):
        self.http = http
        self.key = KEYS["MEXC_API_KEY"]
        self.secret = KEYS["MEXC_API_SECRET"]
        self.filters: "dict[str, SymbolFilters]" = {}

    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        # Подпись создаётся ОДИН раз и переиспользуется при ретраях с
        # бэкоффом, поэтому окно должно покрывать суммарную задержку.
        params["recvWindow"] = 10000
        qs = urlencode(params)
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    async def symbols_usdt(self) -> "dict[str, str]":
        """{symbol: baseAsset} для активных спотовых USDT-пар.
        Попутно заполняет self.filters торговыми ограничениями."""
        data, err = await self.http.get_json(f"{MEXC_BASE}/api/v3/exchangeInfo")
        if err or not data:
            print(f"[MEXC] exchangeInfo error: {err}")
            return {}
        out = {}
        for s in data.get("symbols", []):
            if s.get("quoteAsset") != "USDT":
                continue
            if str(s.get("status")) not in ("1", "ENABLED"):
                continue
            if s.get("isSpotTradingAllowed") is False:
                continue
            sym = s["symbol"]
            out[sym] = s["baseAsset"]
            # quoteAmountPrecision — минимальная сумма ордера в USDT,
            # baseSizePrecision — шаг количества. minOrderQty MEXC не отдаёт.
            self.filters[sym] = SymbolFilters(
                min_qty=0.0,
                min_notional=_f(s.get("quoteAmountPrecision")),
                qty_step=_f(s.get("baseSizePrecision")),
            )
        return out

    async def tickers(self) -> dict:
        """{symbol: {'vol', 'bid', 'ask'}} по 24h тикеру."""
        data, err = await self.http.get_json(f"{MEXC_BASE}/api/v3/ticker/24hr")
        if err or not data:
            print(f"[MEXC] ticker/24hr error: {err}")
            return {}
        out = {}
        for t in data:
            try:
                out[t["symbol"]] = {
                    "vol": float(t.get("quoteVolume") or 0.0),
                    "bid": float(t.get("bidPrice") or 0.0),
                    "ask": float(t.get("askPrice") or 0.0),
                }
            except (TypeError, ValueError):
                continue
        return out

    async def depth(self, symbol: str):
        data, err = await self.http.get_json(
            f"{MEXC_BASE}/api/v3/depth",
            params={"symbol": symbol, "limit": CONFIG["MEXC_DEPTH_LIMIT"]},
        )
        if err or not data:
            return None, None, err
        bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
        asks = [[float(p), float(q)] for p, q in data.get("asks", [])]
        return bids, asks, None

    async def coin_networks(self) -> "Optional[dict[str, CoinNetworks]]":
        """{coin: CoinNetworks}. SIGNED — без ключей вернёт None."""
        if not (self.key and self.secret):
            return None
        params = self._sign({})
        headers = {"X-MEXC-APIKEY": self.key}
        data, err = await self.http.get_json(
            f"{MEXC_BASE}/api/v3/capital/config/getall", params=params, headers=headers
        )
        if err or not isinstance(data, list):
            print(f"[MEXC] capital/config error (сети не проверяются): {err}")
            return None
        out = {}
        for c in data:
            coin = c.get("coin")
            cn = CoinNetworks(coin=coin)
            for n in c.get("networkList", []):
                raw = n.get("netWork") or n.get("network") or n.get("name") or ""
                canon = normalize_network(raw)
                cn.networks[canon] = NetworkInfo(
                    canon=canon, raw_name=raw,
                    deposit=bool(n.get("depositEnable")),
                    withdraw=bool(n.get("withdrawEnable")),
                    withdraw_fee_coin=_f(n.get("withdrawFee")),
                    withdraw_min_coin=_f(n.get("withdrawMin")),
                    deposit_min_coin=_f(n.get("depositMin")),
                    contract=(n.get("contract") or ""),
                )
            out[coin] = cn
        return out


# ============================ BYBIT ============================

class Bybit:
    def __init__(self, http: Http):
        self.http = http
        self.key = KEYS["BYBIT_API_KEY"]
        self.secret = KEYS["BYBIT_API_SECRET"]
        self.filters: "dict[str, SymbolFilters]" = {}

    def _headers(self, query: str) -> dict:
        ts = str(int(time.time() * 1000))
        recv = "10000"
        payload = ts + self.key + recv + query
        sig = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return {
            "X-BAPI-API-KEY": self.key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sig,
        }

    async def symbols_usdt(self) -> "dict[str, str]":
        """{symbol: baseCoin}. Сейчас spot отдаётся одной страницей без
        пагинации; цикл по nextPageCursor — защита на случай её появления.
        Попутно заполняет self.filters из lotSizeFilter."""
        out = {}
        cursor = ""
        while True:
            params = {"category": "spot"}
            if cursor:
                params["cursor"] = cursor
            data, err = await self.http.get_json(
                f"{BYBIT_BASE}/v5/market/instruments-info", params=params
            )
            if err or not data or data.get("retCode") != 0:
                print(f"[Bybit] instruments-info error: {err or data}")
                return out
            res = data["result"]
            for s in res.get("list", []):
                if s.get("quoteCoin") != "USDT":
                    continue
                if s.get("status") != "Trading":
                    continue
                sym = s["symbol"]
                out[sym] = s["baseCoin"]
                lot = s.get("lotSizeFilter") or {}
                self.filters[sym] = SymbolFilters(
                    min_qty=_f(lot.get("minOrderQty")),
                    min_notional=_f(lot.get("minOrderAmt")),
                    qty_step=_f(lot.get("basePrecision")),
                )
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    async def tickers(self) -> dict:
        data, err = await self.http.get_json(
            f"{BYBIT_BASE}/v5/market/tickers", params={"category": "spot"}
        )
        if err or not data or data.get("retCode") != 0:
            print(f"[Bybit] tickers error: {err or data}")
            return {}
        out = {}
        for t in data["result"]["list"]:
            try:
                out[t["symbol"]] = {
                    "vol": float(t.get("turnover24h") or 0.0),
                    "bid": float(t.get("bid1Price") or 0.0),
                    "ask": float(t.get("ask1Price") or 0.0),
                }
            except (TypeError, ValueError):
                continue
        return out

    async def depth(self, symbol: str):
        data, err = await self.http.get_json(
            f"{BYBIT_BASE}/v5/market/orderbook",
            params={"category": "spot", "symbol": symbol,
                    "limit": CONFIG["BYBIT_DEPTH_LIMIT"]},
        )
        if err or not data or data.get("retCode") != 0:
            return None, None, err or (data or {}).get("retMsg")
        res = data["result"]
        bids = [[float(p), float(q)] for p, q in res.get("b", [])]
        asks = [[float(p), float(q)] for p, q in res.get("a", [])]
        return bids, asks, None

    async def coin_networks(self) -> "Optional[dict[str, CoinNetworks]]":
        """SIGNED — без ключей вернёт None. Ключу нужно право Wallet->Read."""
        if not (self.key and self.secret):
            return None
        headers = self._headers("")
        data, err = await self.http.get_json(
            f"{BYBIT_BASE}/v5/asset/coin/query-info", headers=headers
        )
        if err or not data or data.get("retCode") != 0:
            print(f"[Bybit] coin/query-info error (сети не проверяются): "
                  f"{err or (data or {}).get('retMsg')}")
            return None
        out = {}
        for c in data["result"]["rows"]:
            coin = c.get("coin")
            cn = CoinNetworks(coin=coin)
            for ch in c.get("chains", []):
                # 'chain' — канонический код сети, надёжнее для нормализации.
                raw = ch.get("chain") or ch.get("chainType") or ""
                canon = normalize_network(raw)
                cn.networks[canon] = NetworkInfo(
                    canon=canon, raw_name=raw,
                    deposit=(str(ch.get("chainDeposit")) == "1"),
                    withdraw=(str(ch.get("chainWithdraw")) == "1"),
                    withdraw_fee_coin=_f(ch.get("withdrawFee")),
                    withdraw_min_coin=_f(ch.get("withdrawMin")),
                    deposit_min_coin=_f(ch.get("depositMin")),
                    contract=(ch.get("contractAddress") or ""),
                )
            out[coin] = cn
        return out


# ============================ VWAP ============================

def vwap_buy_for_usdt(asks, budget_usdt: float):
    """Тратим budget_usdt по asks. -> (qty, avg_price, spent, полностью_ли)."""
    spent = 0.0
    qty = 0.0
    for price, avail in asks:
        if price <= 0:
            continue
        remaining = budget_usdt - spent
        if remaining <= 0:
            break
        level_cost = price * avail
        if level_cost >= remaining:
            take_qty = remaining / price
            qty += take_qty
            spent += take_qty * price
            return qty, (spent / qty if qty else 0.0), spent, True
        qty += avail
        spent += level_cost
    return qty, (spent / qty if qty else 0.0), spent, False


def vwap_sell_qty(bids, qty_to_sell: float):
    """Продаём qty_to_sell по bids. -> (usdt, avg_price, sold, полностью_ли)."""
    got = 0.0
    sold = 0.0
    for price, avail in bids:
        if price <= 0:
            continue
        remaining = qty_to_sell - sold
        if remaining <= 0:
            break
        take = min(avail, remaining)
        got += take * price
        sold += take
    ok = sold >= qty_to_sell * 0.999999
    return got, (got / sold if sold else 0.0), sold, ok


# ==================== TELEGRAM ====================

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
_tg_last: dict = {}


async def notify_telegram(http, opps):
    """Отправка связок в Telegram. Та же связка - не чаще раза в 30 мин."""
    if not (TG_TOKEN and TG_CHAT):
        return
    now = time.time()
    fresh = []
    for o in opps:
        key = (o.coin, o.direction, o.network)
        if now - _tg_last.get(key, 0) < 1800:
            continue
        _tg_last[key] = now
        fresh.append(o)
    if not fresh:
        return
    lines = ["Найдены связки:"]
    for o in fresh[:10]:
        lines.append(
            f"{o.coin} {o.direction} [{o.network}] "
            f"чистый {o.net_spread_pct:.2f}% ~ {o.net_profit_usdt:.2f}$ "
            f"на {o.spent_usdt:.0f}$"
            + (f" | {o.note}" if o.note else "")
        )
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        async with http.session.post(
            url, json={"chat_id": TG_CHAT, "text": "\n".join(lines)}
        ) as r:
            if r.status != 200:
                print(f"[TG] ошибка отправки: HTTP {r.status}")
    except Exception as e:
        print(f"[TG] ошибка отправки: {e}")


# ============================ ЯДРО РАСЧЁТА ============================

def choose_network(base_coin, src_nets, dst_nets, coin_price_usdt: float,
                   qty_planned: float) -> Optional[NetChoice]:
    """Выбор совместимой сети с минимальной стоимостью вывода. Учитывает
    deposit/withdraw, сверку контрактов, withdrawMin и depositMin получателя."""
    if src_nets is None or dst_nets is None:
        fee_usdt = CONFIG["FALLBACK_WITHDRAW_FEE_USDT"]
        fee_coin = fee_usdt / coin_price_usdt if coin_price_usdt > 0 else 0.0
        return NetChoice("?(не проверено)", fee_coin, fee_usdt,
                         verified=False, contract_checked=False)

    src = src_nets.get(base_coin)
    dst = dst_nets.get(base_coin)
    if not src or not dst:
        return None

    best: Optional[NetChoice] = None
    for canon, sinfo in src.networks.items():
        dinfo = dst.networks.get(canon)
        if not dinfo:
            continue
        if not sinfo.withdraw or not dinfo.deposit:
            continue
        # Сверка контрактов возможна, только если оба указаны.
        contract_checked = bool(sinfo.contract and dinfo.contract)
        if contract_checked and sinfo.contract.lower() != dinfo.contract.lower():
            # Разные контракты при одном тикере = разные токены. Пропуск.
            continue
        # Минимальный объём вывода на бирже-отправителе.
        if sinfo.withdraw_min_coin > 0 and qty_planned < sinfo.withdraw_min_coin:
            continue
        # Минимальный депозит на бирже-получателе (придёт объём минус fee).
        arriving = qty_planned - sinfo.withdraw_fee_coin
        if dinfo.deposit_min_coin > 0 and arriving < dinfo.deposit_min_coin:
            continue
        fee_usdt = sinfo.withdraw_fee_coin * coin_price_usdt
        if best is None or fee_usdt < best.fee_usdt:
            best = NetChoice(canon, sinfo.withdraw_fee_coin, fee_usdt,
                             verified=True, contract_checked=contract_checked)
    return best


def compute_direction(coin, direction, buy_depth, sell_depth,
                      taker_buy, taker_sell, src_nets, dst_nets,
                      buy_filt: Optional[SymbolFilters],
                      sell_filt: Optional[SymbolFilters]):
    """Одна сторона арбитража. buy_depth/sell_depth = (bids, asks)."""
    _, buy_asks = buy_depth
    sell_bids, _ = sell_depth
    if not buy_asks or not sell_bids:
        return None

    budget = CONFIG["POSITION_USDT"]
    qty, buy_vwap, spent, buy_full = vwap_buy_for_usdt(buy_asks, budget)
    if qty <= 0 or buy_vwap <= 0 or spent <= 0:
        return None
    # Минимальная сумма ордера на бирже покупки.
    if buy_filt and buy_filt.min_notional > 0 and spent < buy_filt.min_notional:
        return None
    qty_after_fee = qty * (1 - taker_buy)

    net = choose_network(coin, src_nets, dst_nets, buy_vwap, qty_after_fee)
    if net is None:
        return None

    qty_to_sell = qty_after_fee - net.fee_coin
    if qty_to_sell <= 0:
        return None
    # Ограничения биржи продажи: шаг количества и минимумы.
    if sell_filt:
        qty_to_sell = floor_step(qty_to_sell, sell_filt.qty_step)
        if qty_to_sell <= 0:
            return None
        if sell_filt.min_qty > 0 and qty_to_sell < sell_filt.min_qty:
            return None

    usdt_gross, sell_vwap, sold, sell_full = vwap_sell_qty(sell_bids, qty_to_sell)
    if sell_vwap <= 0:
        return None
    if (sell_filt and sell_filt.min_notional > 0
            and usdt_gross < sell_filt.min_notional):
        return None
    usdt_out = usdt_gross * (1 - taker_sell)

    # База расчёта — фактически потраченное (spent), а не полный бюджет.
    net_profit = usdt_out - spent
    net_spread = net_profit / spent * 100.0
    gross_spread = (sell_vwap - buy_vwap) / buy_vwap * 100.0

    note = ""
    if not net.verified:
        note += "сеть/fee НЕ проверены; "
    elif not net.contract_checked:
        note += "контракты не сверены (нет данных у биржи); "
    if not buy_full:
        note += f"стакан покупки вместил только {spent:.0f}$; "
    if not sell_full:
        # Часть купленного продать некуда — результат недостоверен.
        note += "стакан продажи мельче объёма, прибыль недостоверна; "

    return Opportunity(
        coin=coin, direction=direction, network=net.canon,
        net_spread_pct=net_spread, gross_spread_pct=gross_spread,
        net_profit_usdt=net_profit, spent_usdt=spent,
        buy_vwap=buy_vwap, sell_vwap=sell_vwap,
        wd_fee_usdt=net.fee_usdt,
        depth_ok=(buy_full and sell_full), note=note.strip(),
    )


# ============================ СКАНИРОВАНИЕ ============================

def ticker_spread_pct(buy_ask: float, sell_bid: float) -> float:
    if buy_ask <= 0 or sell_bid <= 0:
        return -100.0
    return (sell_bid - buy_ask) / buy_ask * 100.0


async def scan_once(mexc: Mexc, bybit: Bybit, m_nets, b_nets):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Загрузка пар и тикеров...")
    m_syms, b_syms, m_tick, b_tick = await asyncio.gather(
        mexc.symbols_usdt(), bybit.symbols_usdt(),
        mexc.tickers(), bybit.tickers(),
    )
    if not m_syms or not b_syms:
        print("Не удалось получить пары. Пропуск итерации.")
        return

    m_base = {base: sym for sym, base in m_syms.items()}
    b_base = {base: sym for sym, base in b_syms.items()}
    common = set(m_base) & set(b_base)
    print(f"Общих USDT-монет: {len(common)}")

    vmin = CONFIG["MIN_24H_QUOTE_VOLUME"]
    smin = CONFIG["MIN_GROSS_SPREAD_PCT"]
    smax = CONFIG["MAX_GROSS_SPREAD_PCT"]

    # ПРЕ-ФИЛЬТР: оборот + сырой спред по тикерам, КАЖДОЕ направление отдельно.
    # Аномалия в одну сторону не выбрасывает монету, если вторая сторона в норме.
    cands = []
    ticker_anomalies = 0
    for base in common:
        ms, bs = m_base[base], b_base[base]
        mt, bt = m_tick.get(ms), b_tick.get(bs)
        if not mt or not bt:
            continue
        if mt["vol"] < vmin or bt["vol"] < vmin:
            continue
        s1 = ticker_spread_pct(mt["ask"], bt["bid"])  # MEXC->Bybit
        s2 = ticker_spread_pct(bt["ask"], mt["bid"])  # Bybit->MEXC
        if s1 > smax or s2 > smax:
            ticker_anomalies += 1
        valid = [s for s in (s1, s2) if smin <= s <= smax]
        if valid:
            cands.append((base, ms, bs, max(valid)))
    cands.sort(key=lambda x: x[3], reverse=True)
    cands = cands[: CONFIG["MAX_CANDIDATES"]]
    print(f"Кандидатов после фильтра оборота и спреда: {len(cands)} "
          f"(направлений с аномалией по тикерам: {ticker_anomalies})")

    anomalies = 0
    depth_errors = 0
    last_depth_err = ""

    async def process(base, ms, bs):
        nonlocal anomalies, depth_errors, last_depth_err
        m_depth, b_depth = await asyncio.gather(mexc.depth(ms), bybit.depth(bs))
        m_bids, m_asks, m_err = m_depth
        b_bids, b_asks, b_err = b_depth
        if m_err or b_err or m_bids is None or b_bids is None:
            depth_errors += 1
            last_depth_err = f"{base}: {m_err or b_err}"
            return []
        mf, bf = mexc.filters.get(ms), bybit.filters.get(bs)
        res = []
        o1 = compute_direction(
            base, "MEXC->Bybit",
            buy_depth=(m_bids, m_asks), sell_depth=(b_bids, b_asks),
            taker_buy=CONFIG["TAKER_FEE_MEXC"], taker_sell=CONFIG["TAKER_FEE_BYBIT"],
            src_nets=m_nets, dst_nets=b_nets,
            buy_filt=mf, sell_filt=bf,
        )
        o2 = compute_direction(
            base, "Bybit->MEXC",
            buy_depth=(b_bids, b_asks), sell_depth=(m_bids, m_asks),
            taker_buy=CONFIG["TAKER_FEE_BYBIT"], taker_sell=CONFIG["TAKER_FEE_MEXC"],
            src_nets=b_nets, dst_nets=m_nets,
            buy_filt=bf, sell_filt=mf,
        )
        for o in (o1, o2):
            if not o or o.net_spread_pct < CONFIG["MIN_NET_SPREAD_PCT"]:
                continue
            if o.gross_spread_pct > CONFIG["MAX_GROSS_SPREAD_PCT"]:
                anomalies += 1
                continue
            res.append(o)
        return res

    chunks = await asyncio.gather(*[process(b, ms, bs) for b, ms, bs, _ in cands])
    opps = [o for ch in chunks for o in ch]
    opps.sort(key=lambda o: o.net_spread_pct, reverse=True)
    await notify_telegram(mexc.http, opps)

    if depth_errors:
        print(f"!!! Ошибок при загрузке стаканов: {depth_errors} из {len(cands)} "
              f"кандидатов (последняя: {last_depth_err}). "
              f"Ретраев HTTP за сессию: {mexc.http.retried}.")
    print_table(opps, anomalies)


def print_table(opps, anomalies: int):
    if anomalies:
        print(f"Отброшено аномальных спредов по стакану "
              f"(> {CONFIG['MAX_GROSS_SPREAD_PCT']}%): {anomalies} — "
              f"обычно это стоп deposit/withdraw или разные токены.")
    if not opps:
        print("Возможностей выше порога не найдено.")
        return
    print(f"\n{'МОНЕТА':<10}{'НАПРАВЛЕНИЕ':<15}{'СЕТЬ':<16}"
          f"{'СПРЕД%':>8}{'ПРИБЫЛЬ$':>11}{'ОБЪЁМ$':>9}{'WD_FEE$':>9}  "
          f"{'ГЛУБИНА':<9}ПРИМЕЧАНИЕ")
    print("-" * 110)
    for o in opps:
        depth = "OK" if o.depth_ok else "ЧАСТ."
        print(f"{o.coin:<10}{o.direction:<15}{o.network:<16}"
              f"{o.net_spread_pct:>7.2f}%{o.net_profit_usdt:>11.2f}"
              f"{o.spent_usdt:>9.0f}{o.wd_fee_usdt:>9.2f}  {depth:<9}{o.note}")
    print("-" * 110)
    print(f"Найдено: {len(opps)}. Целевая позиция: {CONFIG['POSITION_USDT']} USDT. "
          f"Порог: {CONFIG['MIN_NET_SPREAD_PCT']}%.")
    print("НАПОМИНАНИЕ: оценка по мгновенному стакану. За время перевода монеты "
          "между биржами цена уйдёт. Статусы deposit/withdraw в API могут "
          "запаздывать. Реальное исполнение будет отличаться.")


async def load_networks(mexc: Mexc, bybit: Bybit):
    m_nets, b_nets = await asyncio.gather(
        mexc.coin_networks(), bybit.coin_networks()
    )
    if m_nets is None or b_nets is None:
        print("!!! ВНИМАНИЕ: сети / deposit-withdraw / комиссии вывода НЕ "
              "проверяются (нет ключей, нет права Wallet->Read у Bybit или "
              "ошибка). Withdrawal fee грубо = "
              f"{CONFIG['FALLBACK_WITHDRAW_FEE_USDT']} USDT (для ERC20 "
              "реальная обычно выше). Каждую находку проверяйте на сайте "
              "бирж вручную перед любыми действиями.")
    return m_nets, b_nets


async def main():
    timeout = aiohttp.ClientTimeout(
        total=CONFIG["HTTP_TIMEOUT"], connect=CONFIG["CONNECT_TIMEOUT"]
    )
    sem = asyncio.Semaphore(CONFIG["MAX_CONCURRENCY"])
    async with aiohttp.ClientSession(timeout=timeout) as session:
        http = Http(session, sem)
        mexc = Mexc(http)
        bybit = Bybit(http)

        print("Загрузка данных о сетях (нужны READ-ключи в переменных окружения)...")
        m_nets, b_nets = await load_networks(mexc, bybit)

        if not CONFIG["LOOP"]:
            await scan_once(mexc, bybit, m_nets, b_nets)
            return

        nets_ts = time.time()
        try:
            while True:
                await scan_once(mexc, bybit, m_nets, b_nets)
                # Периодическое обновление данных о сетях: комиссии и
                # статусы deposit/withdraw меняются в течение дня.
                if time.time() - nets_ts >= CONFIG["NETWORKS_REFRESH_SEC"]:
                    print("Обновление данных о сетях...")
                    m_nets, b_nets = await load_networks(mexc, bybit)
                    nets_ts = time.time()
                print(f"Пауза {CONFIG['LOOP_INTERVAL_SEC']} с...")
                await asyncio.sleep(CONFIG["LOOP_INTERVAL_SEC"])
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")


if __name__ == "__main__":
    asyncio.run(main())
