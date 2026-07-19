"""Telegram bot for Vietnamese equity lookups and deep-value alerts.

Secrets are read from environment variables and are never accepted on the
command line or written to disk. The Google Gen AI SDK is optional at runtime:
without it or without an API key, the quantitative scanner still works.

Commands:
    /start
    /help
    /quote <TICKER>
    /report <TICKER>
    /chart <TICKER>
    /ta <TICKER>
    /deep <TICKER>
    /scan
    /signals_on
    /signals_off
    /signals_status
    /market
    /add <TICKER>
    /remove <TICKER>
    /watchlist
    /watch
    /ping

This is a research/market-data helper, not an investment-advice engine.
Quotes are fetched from Yahoo Finance's public chart endpoint and may be
delayed, incomplete, or unavailable.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote as url_quote
from urllib.request import Request, urlopen

try:
    from google import genai
except ImportError:  # The quantitative scanner still works without the optional SDK.
    genai = None


LOG = logging.getLogger("vn_equity_bot")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}$")
HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
MAX_WATCHLIST_SIZE = 10
DEFAULT_POLL_TIMEOUT = 25
DEFAULT_YAHOO_TIMEOUT = 12
DEFAULT_SCAN_WEEKDAYS = "0,3"
DEFAULT_SCAN_TIME = "20:30"
DEFAULT_MONTHLY_SIGNAL_LIMIT = 2
DEFAULT_SIGNAL_COOLDOWN_DAYS = 30
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_THINKING_LEVEL = "high"
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 1200
DEFAULT_GEMINI_TIMEOUT = 45
DEFAULT_SCAN_WORKERS = 6

VN100_SYMBOLS = [
    "AAA", "ACB", "ANV", "APH", "ASM", "BCM", "BID", "BMP", "BSI", "BVH",
    "CII", "CMG", "CRE", "CTD", "CTG", "DBC", "DCM", "DGC", "DGW", "DHC",
    "DIG", "DPM", "DRC", "DXG", "EIB", "FPT", "FRT", "FTS", "GAS", "GEX",
    "GMD", "GVR", "HAG", "HCM", "HDB", "HDC", "HDG", "HHV", "HPG", "HSG",
    "HT1", "IMP", "KBC", "KDC", "KDH", "KSB", "LCG", "LPB", "MBB", "MSB",
    "MSN", "MWG", "NKG", "NLG", "OCB", "PAN", "PC1", "PDR", "PET", "PGV",
    "PLX", "PNJ", "POW", "PVD", "PVT", "REE", "SAB", "SBT", "SCS", "SHB",
    "SJS", "SSB", "SSI", "STB", "TCB", "TCH", "TMS", "TPB", "VCB", "VCG",
    "VCI", "VGC", "VHC", "VHM", "VIB", "VIC", "VIX", "VJC", "VND", "VNM",
    "VPB", "VPI", "VRE",
]


class BotError(RuntimeError):
    """A user-safe error returned by an upstream service."""


@dataclass(frozen=True)
class Quote:
    """A compact quote model used by the message formatter."""

    symbol: str
    name: str
    price: float
    previous_close: float | None
    currency: str = "VND"
    as_of: str | None = None
    open_price: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: int | None = None

    @property
    def change(self) -> float | None:
        if self.previous_close is None:
            return None
        return self.price - self.previous_close

    @property
    def change_pct(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return (self.price / self.previous_close - 1.0) * 100.0


@dataclass(frozen=True)
class PriceBar:
    """Daily market data used for simple chart and technical summaries."""

    close: float
    high: float | None = None
    low: float | None = None
    open_price: float | None = None
    volume: int | None = None


@dataclass(frozen=True)
class FundamentalSnapshot:
    """Valuation and price fields used by the VN100 signal scanner."""

    symbol: str
    name: str
    price: float | None = None
    trailing_pe: float | None = None
    price_to_book: float | None = None
    market_cap: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None


@dataclass(frozen=True)
class DeepSignal:
    """A candidate that survived the quantitative discount filter."""

    symbol: str
    score: int
    snapshot: FundamentalSnapshot
    quote: Quote
    bars: list[PriceBar]
    reasons: list[str]


def normalize_symbol(raw: str) -> str:
    """Validate and normalize a Vietnamese ticker supplied by a user."""

    symbol = raw.strip().upper()
    # Allow users to paste a Yahoo-style suffix, while storing only the
    # exchange ticker in their watchlist.
    if symbol.endswith(".VN"):
        symbol = symbol[:-3]
    if symbol.startswith("^"):
        # Index symbols are accepted only by the explicit market command.
        raise ValueError("Mã cổ phiếu không hợp lệ.")
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("Mã phải gồm 1–10 ký tự chữ/số, ví dụ FPT hoặc VNM.")
    return symbol


def yahoo_symbol(symbol: str) -> str:
    """Map an exchange ticker to Yahoo Finance's chart symbol."""

    normalized = symbol.upper()
    if normalized == "VNINDEX":
        # Yahoo lists the index on the Vietnam exchange suffix.
        return "^VNINDEX.VN"
    if normalized == "VN30":
        return "^VN30.VN"
    if normalized.startswith("^") or "." in normalized:
        return normalized
    return f"{normalized}.VN"


def _json_request(url: str, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "vn-equity-bot/1.0 (+https://github.com/)",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise BotError("Không lấy được dữ liệu thị trường lúc này.") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BotError("Nguồn dữ liệu trả về định dạng không hợp lệ.") from exc
    if not isinstance(payload, dict):
        raise BotError("Nguồn dữ liệu trả về định dạng không hợp lệ.")
    return payload


def _json_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "vn-equity-bot/1.0 (+https://github.com/)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise BotError("Không lấy được dữ liệu thị trường lúc này.") from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BotError("Nguồn dữ liệu trả về định dạng không hợp lệ.") from exc
    if not isinstance(result, dict):
        raise BotError("Nguồn dữ liệu trả về định dạng không hợp lệ.")
    return result


def _first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, dict):
            value = value.get("raw") or value.get("fmt")
        if value in (None, "", "N/A"):
            continue
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


class YahooQuoteProvider:
    """Small, cache-aware Yahoo Finance chart client."""

    def __init__(self, timeout: float = DEFAULT_YAHOO_TIMEOUT, cache_ttl: float = 60.0):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._history_cache: dict[tuple[str, str, str], tuple[float, list[PriceBar]]] = {}
        self._fundamental_cache: dict[str, tuple[float, FundamentalSnapshot]] = {}

    def get_quote(self, symbol: str) -> Quote:
        normalized = symbol.upper()
        cached = self._cache.get(normalized)
        now = time.monotonic()
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        yahoo = yahoo_symbol(normalized)
        endpoint = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{url_quote(yahoo, safe='')}"
            "?range=5d&interval=1d&includePrePost=false"
        )
        payload = _json_request(endpoint, self.timeout)
        try:
            result = payload["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice")
            if price is None:
                quote = result["indicators"]["quote"][0]
                closes = [x for x in quote.get("close", []) if x is not None]
                price = closes[-1] if closes else None
            if price is None:
                raise KeyError("price")
            previous = (
                meta.get("previousClose")
                or meta.get("chartPreviousClose")
                or meta.get("regularMarketPreviousClose")
            )
            quote_data = result["indicators"]["quote"][0]
            highs = [x for x in quote_data.get("high", []) if x is not None]
            lows = [x for x in quote_data.get("low", []) if x is not None]
            opens = [x for x in quote_data.get("open", []) if x is not None]
            volumes = [x for x in quote_data.get("volume", []) if x is not None]
            display_symbol = "VN-Index" if normalized == "VNINDEX" else normalized
            quote = Quote(
                symbol=normalized,
                name=str(meta.get("longName") or meta.get("shortName") or display_symbol),
                price=float(price),
                previous_close=float(previous) if previous is not None else None,
                currency=str(meta.get("currency") or "VND"),
                as_of=str(meta.get("regularMarketTime") or "") or None,
                open_price=float(opens[-1]) if opens else None,
                day_high=float(highs[-1]) if highs else None,
                day_low=float(lows[-1]) if lows else None,
                volume=int(volumes[-1]) if volumes else None,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BotError(f"Chưa có dữ liệu cho mã {normalized}.") from exc
        self._cache[normalized] = (now, quote)
        return quote

    def get_history(self, symbol: str, range_value: str = "6mo", interval: str = "1d") -> list[PriceBar]:
        normalized = symbol.upper()
        cache_key = (normalized, range_value, interval)
        cached = self._history_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        yahoo = yahoo_symbol(normalized)
        endpoint = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{url_quote(yahoo, safe='')}"
            f"?range={url_quote(range_value)}&interval={url_quote(interval)}&includePrePost=false"
        )
        payload = _json_request(endpoint, self.timeout)
        try:
            result = payload["chart"]["result"][0]
            quote_data = result["indicators"]["quote"][0]
            closes = quote_data.get("close", [])
            highs = quote_data.get("high", [])
            lows = quote_data.get("low", [])
            opens = quote_data.get("open", [])
            volumes = quote_data.get("volume", [])
            bars: list[PriceBar] = []
            for index, close in enumerate(closes):
                if close is None:
                    continue
                bars.append(
                    PriceBar(
                        close=float(close),
                        high=float(highs[index]) if index < len(highs) and highs[index] is not None else None,
                        low=float(lows[index]) if index < len(lows) and lows[index] is not None else None,
                        open_price=float(opens[index]) if index < len(opens) and opens[index] is not None else None,
                        volume=int(volumes[index]) if index < len(volumes) and volumes[index] is not None else None,
                    )
                )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BotError(f"Chưa có dữ liệu lịch sử cho mã {normalized}.") from exc
        if len(bars) < 2:
            raise BotError(f"Chưa đủ dữ liệu lịch sử cho mã {normalized}.")
        self._history_cache[cache_key] = (now, bars)
        return bars

    def get_fundamentals(self, symbol: str) -> FundamentalSnapshot:
        normalized = symbol.upper()
        snapshots = self.get_fundamentals_batch([normalized])
        if normalized not in snapshots:
            raise BotError(f"Chưa có dữ liệu định giá cho mã {normalized}.")
        return snapshots[normalized]

    def get_fundamentals_batch(
        self,
        symbols: Iterable[str],
    ) -> dict[str, FundamentalSnapshot]:
        """Fetch many TradingView rows in one request and populate the cache."""

        normalized_symbols = list(dict.fromkeys(str(item).upper() for item in symbols))
        now = time.monotonic()
        snapshots: dict[str, FundamentalSnapshot] = {}
        missing: list[str] = []
        for symbol in normalized_symbols:
            cached = self._fundamental_cache.get(symbol)
            if cached and now - cached[0] < self.cache_ttl:
                snapshots[symbol] = cached[1]
            else:
                missing.append(symbol)
        if not missing:
            return snapshots

        payload = _json_post(
            "https://scanner.tradingview.com/vietnam/scan",
            {
                "symbols": {
                    "tickers": [f"HOSE:{symbol}" for symbol in missing],
                    "query": {"types": []},
                },
                "columns": [
                    "name",
                    "description",
                    "close",
                    "price_earnings_ttm",
                    "price_book_fq",
                    "market_cap_basic",
                ],
            },
            self.timeout,
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise BotError("Nguồn định giá trả về dữ liệu không hợp lệ.")
        for row in rows:
            if not isinstance(row, dict):
                continue
            values = row.get("d")
            ticker = str(row.get("s") or "").rsplit(":", 1)[-1].upper()
            if not ticker and isinstance(values, list) and values:
                ticker = str(values[0] or "").upper()
            if ticker not in missing or not isinstance(values, list) or len(values) < 6:
                continue
            snapshot = FundamentalSnapshot(
                symbol=ticker,
                name=str(values[1] or values[0] or ticker),
                price=_first_number(values[2]),
                trailing_pe=_first_number(values[3]),
                price_to_book=_first_number(values[4]),
                market_cap=_first_number(values[5]),
            )
            snapshots[ticker] = snapshot
            self._fundamental_cache[ticker] = (now, snapshot)
        return snapshots


def format_number(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}"


def format_quote(quote: Quote) -> str:
    """Render a quote as Telegram HTML without trusting upstream text."""

    change = quote.change
    change_pct = quote.change_pct
    if change is None or change_pct is None:
        change_line = "Thay đổi: —"
    else:
        sign = "+" if change >= 0 else ""
        change_line = (
            f"Thay đổi: <b>{sign}{format_number(change)}</b> "
            f"({sign}{change_pct:.2f}%)"
        )
    return (
        f"<b>{html.escape(quote.symbol)}</b> — {html.escape(quote.name)}\n"
        f"Giá: <b>{format_number(quote.price)}</b> {html.escape(quote.currency)}\n"
        f"{change_line}\n"
        "<i>Gõ /report "
        f"{html.escape(quote.symbol)} để xem thêm. Dữ liệu tham khảo, có thể trễ hoặc thiếu.</i>"
    )


def format_report(quote: Quote) -> str:
    """Render a richer single-symbol snapshot."""

    change = quote.change
    change_pct = quote.change_pct
    if change is None or change_pct is None:
        change_text = "—"
    else:
        sign = "+" if change >= 0 else ""
        change_text = f"{sign}{format_number(change)} ({sign}{change_pct:.2f}%)"
    return (
        f"<b>Báo cáo nhanh: {html.escape(quote.symbol)}</b>\n"
        f"{html.escape(quote.name)}\n\n"
        f"Giá hiện tại: <b>{format_number(quote.price)}</b> {html.escape(quote.currency)}\n"
        f"Thay đổi: <b>{change_text}</b>\n"
        f"Mở cửa: {format_number(quote.open_price)}\n"
        f"Cao nhất phiên: {format_number(quote.day_high)}\n"
        f"Thấp nhất phiên: {format_number(quote.day_low)}\n"
        f"Khối lượng: {format_number(float(quote.volume), 0) if quote.volume is not None else '—'}\n\n"
        "<i>Dữ liệu từ Yahoo Finance, chỉ để tham khảo.</i>"
    )


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) <= window:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values[-window - 1 : -1], values[-window:]):
        change = current - previous
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    average_gain = sum(gains) / window
    average_loss = sum(losses) / window
    if average_loss == 0:
        return 100.0
    rs = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sparkline(values: list[float], width: int = 30) -> str:
    points = values[-width:]
    if not points:
        return ""
    ticks = "▁▂▃▄▅▆▇█"
    low = min(points)
    high = max(points)
    if high == low:
        return ticks[0] * len(points)
    return "".join(ticks[round((value - low) / (high - low) * (len(ticks) - 1))] for value in points)


def format_chart(symbol: str, bars: list[PriceBar]) -> str:
    closes = [bar.close for bar in bars]
    recent = closes[-30:]
    current = recent[-1]
    low = min(recent)
    high = max(recent)
    change = current - recent[0]
    change_pct = (current / recent[0] - 1.0) * 100.0 if recent[0] else 0.0
    sign = "+" if change >= 0 else ""
    return (
        f"<b>Chart nhanh: {html.escape(symbol)}</b>\n"
        f"<pre>{html.escape(sparkline(closes))}</pre>\n"
        f"30 phiên: {format_number(low)} → {format_number(high)}\n"
        f"Hiện tại: <b>{format_number(current)}</b>\n"
        f"Biến động 30 phiên: <b>{sign}{format_number(change)}</b> ({sign}{change_pct:.2f}%)\n\n"
        "<i>Biểu đồ chữ để xem nhanh trong Telegram.</i>"
    )


def format_ta(symbol: str, bars: list[PriceBar]) -> str:
    closes = [bar.close for bar in bars]
    current = closes[-1]
    ma5 = moving_average(closes, 5)
    ma20 = moving_average(closes, 20)
    rsi14 = rsi(closes, 14)
    recent = bars[-20:]
    support_values = [bar.low for bar in recent if bar.low is not None]
    resistance_values = [bar.high for bar in recent if bar.high is not None]
    support = min(support_values) if support_values else min(closes[-20:])
    resistance = max(resistance_values) if resistance_values else max(closes[-20:])

    if ma5 is not None and ma20 is not None:
        trend = "ngắn hạn tích cực" if ma5 >= ma20 else "ngắn hạn yếu hơn"
    else:
        trend = "chưa đủ dữ liệu"

    if rsi14 is None:
        rsi_note = "chưa đủ dữ liệu"
    elif rsi14 >= 70:
        rsi_note = "RSI cao, có thể đang nóng"
    elif rsi14 <= 30:
        rsi_note = "RSI thấp, có thể đang quá bán"
    else:
        rsi_note = "RSI trung tính"

    return (
        f"<b>TA nhanh: {html.escape(symbol)}</b>\n"
        f"Giá: <b>{format_number(current)}</b>\n"
        f"MA5: {format_number(ma5)}\n"
        f"MA20: {format_number(ma20)}\n"
        f"RSI14: {format_number(rsi14)} — {html.escape(rsi_note)}\n"
        f"Hỗ trợ gần: {format_number(support)}\n"
        f"Kháng cự gần: {format_number(resistance)}\n"
        f"Nhận xét: {html.escape(trend)}.\n\n"
        "<i>Chỉ là thống kê kỹ thuật tự động, không phải khuyến nghị mua/bán.</i>"
    )


class WatchlistStore:
    """Persist per-chat watchlists using an atomic JSON replacement."""

    def __init__(self, path: Path, max_size: int = MAX_WATCHLIST_SIZE):
        self.path = path
        self.max_size = max_size
        self._data: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("Watchlist file is unreadable; starting empty.")
            return
        if not isinstance(raw, dict):
            return
        for chat_id, values in raw.items():
            if not isinstance(values, list):
                continue
            cleaned: list[str] = []
            for value in values:
                try:
                    symbol = normalize_symbol(str(value))
                except ValueError:
                    continue
                if symbol not in cleaned:
                    cleaned.append(symbol)
            self._data[str(chat_id)] = cleaned[: self.max_size]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f"{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def get(self, chat_id: str | int) -> list[str]:
        return list(self._data.get(str(chat_id), []))

    def add(self, chat_id: str | int, symbol: str) -> tuple[bool, str]:
        key = str(chat_id)
        values = self._data.setdefault(key, [])
        if symbol in values:
            return False, f"{symbol} đã có trong danh sách theo dõi."
        if len(values) >= self.max_size:
            return False, f"Danh sách tối đa {self.max_size} mã."
        values.append(symbol)
        self._save()
        return True, f"Đã thêm {symbol}."

    def remove(self, chat_id: str | int, symbol: str) -> tuple[bool, str]:
        key = str(chat_id)
        values = self._data.get(key, [])
        if symbol not in values:
            return False, f"{symbol} không có trong danh sách."
        values.remove(symbol)
        if values:
            self._data[key] = values
        else:
            self._data.pop(key, None)
        self._save()
        return True, f"Đã bỏ {symbol}."


class SignalStore:
    """Persist Telegram chats and cooldowns for deep-value alerts."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, Any] = {"chats": [], "sent": [], "last_scan_date": ""}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("Signal state file is unreadable; starting empty.")
            return
        if not isinstance(raw, dict):
            return
        chats = raw.get("chats", [])
        sent = raw.get("sent", [])
        self._data = {
            "chats": [str(chat) for chat in chats if str(chat).strip()],
            "sent": sent if isinstance(sent, list) else [],
            "last_scan_date": str(raw.get("last_scan_date") or ""),
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f"{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def subscribe(self, chat_id: str | int) -> bool:
        chat = str(chat_id)
        chats = self._data.setdefault("chats", [])
        if chat in chats:
            return False
        chats.append(chat)
        self._save()
        return True

    def unsubscribe(self, chat_id: str | int) -> bool:
        chat = str(chat_id)
        chats = self._data.setdefault("chats", [])
        if chat not in chats:
            return False
        chats.remove(chat)
        self._save()
        return True

    def chats(self) -> list[str]:
        return list(self._data.get("chats", []))

    def last_scan_date(self) -> str:
        return str(self._data.get("last_scan_date") or "")

    def mark_scan(self, date_key: str) -> None:
        self._data["last_scan_date"] = date_key
        self._save()

    def sent_this_month(self, now: datetime) -> int:
        month = now.strftime("%Y-%m")
        return sum(1 for item in self._data.get("sent", []) if str(item.get("month")) == month)

    def recently_sent(self, symbol: str, now: datetime, cooldown_days: int) -> bool:
        cutoff = time.mktime(now.timetuple()) - cooldown_days * 86400
        for item in self._data.get("sent", []):
            if str(item.get("symbol")) != symbol:
                continue
            try:
                sent_at = float(item.get("sent_at"))
            except (TypeError, ValueError):
                continue
            if sent_at >= cutoff:
                return True
        return False

    def record_sent(self, symbol: str, now: datetime) -> None:
        sent = self._data.setdefault("sent", [])
        sent.append(
            {
                "symbol": symbol,
                "sent_at": time.mktime(now.timetuple()),
                "month": now.strftime("%Y-%m"),
            }
        )
        # Keep the file small; only recent state matters for this scanner.
        self._data["sent"] = sent[-200:]
        self._save()


class GeminiAnalyzer:
    """Grounded Gemini research for candidates that pass the numeric filter."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL,
        thinking_level: str = DEFAULT_GEMINI_THINKING_LEVEL,
        max_output_tokens: int = DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
        use_google_search: bool = True,
        timeout: float = DEFAULT_GEMINI_TIMEOUT,
        client_factory: Callable[[str], Any] | None = None,
    ):
        self.api_key = api_key.strip()
        self.model = self._normalize_model(model, DEFAULT_GEMINI_MODEL)
        self.fallback_model = self._normalize_model(
            fallback_model,
            DEFAULT_GEMINI_FALLBACK_MODEL,
        )
        level = thinking_level.strip().lower()
        self.thinking_level = level if level in {"minimal", "low", "medium", "high"} else "high"
        self.max_output_tokens = max(200, min(int(max_output_tokens), 4096))
        self.use_google_search = bool(use_google_search)
        self.timeout = max(5.0, float(timeout))
        self._client_factory = client_factory

    @staticmethod
    def _normalize_model(model: str, default: str) -> str:
        normalized = str(model or "").strip()
        if normalized.startswith("models/"):
            normalized = normalized[len("models/") :]
        return normalized or default

    def enabled(self) -> bool:
        return bool(self.api_key)

    def status_text(self) -> str:
        if not self.enabled():
            return "chưa có API key"
        search = "Google Search bật" if self.use_google_search else "Google Search tắt"
        return f"đã bật ({self.model}, {self.thinking_level}, {search})"

    def _build_prompt(self, signal: DeepSignal) -> str:
        snapshot = signal.snapshot
        quote = signal.quote
        closes = [bar.close for bar in signal.bars]
        high_values = [bar.high for bar in signal.bars if bar.high is not None]
        low_values = [bar.low for bar in signal.bars if bar.low is not None]
        high_52w = snapshot.fifty_two_week_high or (max(high_values) if high_values else None)
        low_52w = snapshot.fifty_two_week_low or (min(low_values) if low_values else None)
        return (
            "Bạn là chuyên viên hỗ trợ nghiên cứu cổ phiếu Việt Nam. Dữ liệu định lượng "
            "bên dưới do hệ thống cung cấp; không được tự sửa, suy diễn hoặc bịa số còn thiếu. "
            "Hãy dùng Google Search để kiểm tra thông tin mới nhất từ nguồn sơ cấp/đáng tin "
            "(công bố doanh nghiệp, HOSE/HNX/SSC, báo cáo tài chính hoặc báo chí tài chính uy tín). "
            "Phân biệt rõ dữ kiện đã kiểm chứng với nhận định. Nếu không tìm được dữ liệu mới, "
            "hãy nói thẳng là chưa đủ dữ liệu. Không khẳng định lợi nhuận và không ra lệnh mua/bán.\n\n"
            f"Ngày phân tích: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"Mã: {signal.symbol}\n"
            f"Tên: {snapshot.name}\n"
            f"Giá: {quote.price}\n"
            f"P/E: {snapshot.trailing_pe}\n"
            f"P/B: {snapshot.price_to_book}\n"
            f"Đỉnh 52 tuần: {high_52w}\n"
            f"Đáy 52 tuần: {low_52w}\n"
            f"MA20: {moving_average(closes, 20)}\n"
            f"RSI14: {rsi(closes, 14)}\n"
            f"Điểm lọc: {signal.score}/100\n"
            f"Lý do lọc: {', '.join(signal.reasons)}\n\n"
            "Trả lời tiếng Việt ngắn gọn theo đúng 6 mục, mỗi mục 1–2 câu: "
            "1) Định giá; 2) Xu hướng giá; 3) Kết quả kinh doanh/tin mới đã kiểm chứng; "
            "4) Chất xúc tác; 5) Rủi ro lớn nhất; 6) Điều kiện để đưa vào vùng theo dõi. "
            "Không chèn bảng và không tự viết danh sách nguồn vì hệ thống sẽ gắn nguồn."
        )

    def _create_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self.api_key)
        if genai is None:
            raise RuntimeError("google-genai chưa được cài đặt")
        return genai.Client(api_key=self.api_key)

    @staticmethod
    def _extract_interaction(interaction: Any) -> tuple[str, list[tuple[str, str]]]:
        text = str(getattr(interaction, "output_text", "") or "").strip()
        sources: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for step in getattr(interaction, "steps", []) or []:
            if getattr(step, "type", "") != "model_output":
                continue
            for block in getattr(step, "content", []) or []:
                if not text:
                    block_text = str(getattr(block, "text", "") or "").strip()
                    if block_text:
                        text = block_text
                for annotation in getattr(block, "annotations", []) or []:
                    url = str(
                        getattr(annotation, "url", "")
                        or getattr(annotation, "uri", "")
                        or ""
                    ).strip()
                    if not HTTP_URL_RE.match(url) or url in seen_urls:
                        continue
                    title = str(getattr(annotation, "title", "") or "Nguồn").strip()
                    sources.append((title[:80], url))
                    seen_urls.add(url)
                    if len(sources) >= 3:
                        break
                if len(sources) >= 3:
                    break
            if len(sources) >= 3:
                break
        return text, sources

    @staticmethod
    def _with_sources(text: str, sources: list[tuple[str, str]]) -> str:
        clean_text = text.strip()[:2200]
        if not sources:
            return clean_text
        source_lines = ["Nguồn kiểm chứng:"]
        for title, url in sources:
            source_lines.append(f"• {title}: {url}")
        return (clean_text + "\n\n" + "\n".join(source_lines))[:3200]

    def _analyze_interactions(self, prompt: str) -> str:
        client = self._create_client()
        tools = [{"type": "google_search"}] if self.use_google_search else None
        interaction = client.interactions.create(
            model=self.model,
            input=prompt,
            tools=tools,
            generation_config={
                "temperature": 1.0,
                "max_output_tokens": self.max_output_tokens,
                "top_p": 0.95,
                "thinking_level": self.thinking_level,
            },
            store=False,
            timeout=self.timeout,
        )
        text, sources = self._extract_interaction(interaction)
        if not text:
            raise RuntimeError("Gemini không trả về nội dung")
        return self._with_sources(text, sources)

    def _analyze_legacy_fallback(self, prompt: str) -> str:
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{url_quote(self.fallback_model, safe='')}:generateContent"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 1.0,
                "maxOutputTokens": min(self.max_output_tokens, 1200),
                "topP": 0.95,
            },
        }
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        result = json.loads(raw.decode("utf-8"))
        parts = result["candidates"][0]["content"]["parts"]
        text = "\n".join(str(part.get("text", "")).strip() for part in parts).strip()
        if not text:
            raise RuntimeError("Gemini fallback không trả về nội dung")
        return text[:2200]

    def analyze(self, signal: DeepSignal) -> str:
        if not self.enabled():
            return "Gemini chưa có API key; bot đang dùng phần chấm điểm định lượng."
        prompt = self._build_prompt(signal)
        try:
            return self._analyze_interactions(prompt)
        except Exception as primary_exc:  # SDK/API failures must not stop Telegram polling.
            LOG.warning(
                "Gemini Interactions failed for %s with model %s: %s",
                signal.symbol,
                self.model,
                type(primary_exc).__name__,
            )
        try:
            return self._analyze_legacy_fallback(prompt)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            RuntimeError,
        ) as fallback_exc:
            LOG.warning(
                "Gemini fallback failed for %s with model %s: %s",
                signal.symbol,
                self.fallback_model,
                type(fallback_exc).__name__,
            )
            return "Gemini không phản hồi lúc này; bot chỉ gửi phần chấm điểm định lượng."


def parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return list(VN100_SYMBOLS)
    symbols: list[str] = []
    for item in re.split(r"[\s,;]+", raw):
        if not item:
            continue
        try:
            symbol = normalize_symbol(item)
        except ValueError:
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols or list(VN100_SYMBOLS)


def score_candidate(
    snapshot: FundamentalSnapshot,
    quote: Quote,
    bars: list[PriceBar],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    price = snapshot.price or quote.price
    high_values = [bar.high for bar in bars if bar.high is not None]
    low_values = [bar.low for bar in bars if bar.low is not None]
    high_52w = snapshot.fifty_two_week_high or (max(high_values) if high_values else None)
    low_52w = snapshot.fifty_two_week_low or (min(low_values) if low_values else None)
    if high_52w and price:
        drawdown_pct = (price / high_52w - 1.0) * 100.0
        if drawdown_pct <= -30:
            score += 30
            reasons.append(f"chiết khấu {drawdown_pct:.1f}% so với đỉnh 52 tuần")
        elif drawdown_pct <= -20:
            score += 20
            reasons.append(f"chiết khấu {drawdown_pct:.1f}% so với đỉnh 52 tuần")
    if low_52w and high_52w and price and high_52w > low_52w:
        range_position = (price - low_52w) / (high_52w - low_52w)
        if range_position <= 0.35:
            score += 10
            reasons.append("giá đang ở nửa thấp của biên 52 tuần")
    if snapshot.trailing_pe is not None:
        if 0 < snapshot.trailing_pe <= 10:
            score += 25
            reasons.append(f"P/E thấp ({snapshot.trailing_pe:.2f})")
        elif 10 < snapshot.trailing_pe <= 15:
            score += 15
            reasons.append(f"P/E hợp lý ({snapshot.trailing_pe:.2f})")
    if snapshot.price_to_book is not None:
        if 0 < snapshot.price_to_book <= 1.2:
            score += 25
            reasons.append(f"P/B thấp ({snapshot.price_to_book:.2f})")
        elif 1.2 < snapshot.price_to_book <= 1.8:
            score += 15
            reasons.append(f"P/B chưa quá cao ({snapshot.price_to_book:.2f})")
    closes = [bar.close for bar in bars]
    rsi14 = rsi(closes, 14)
    ma20 = moving_average(closes, 20)
    if rsi14 is not None:
        if rsi14 <= 35:
            score += 15
            reasons.append(f"RSI14 thấp ({rsi14:.2f})")
        elif rsi14 <= 45:
            score += 8
            reasons.append(f"RSI14 hạ nhiệt ({rsi14:.2f})")
    if ma20 and price and price >= ma20 * 0.97:
        score += 5
        reasons.append("giá không quá xa MA20")
    if not reasons:
        reasons.append("chưa có chiết khấu/định giá đủ nổi bật")
    return min(score, 100), reasons


def format_deep_signal(signal: DeepSignal, gemini_text: str) -> str:
    snapshot = signal.snapshot
    quote = signal.quote
    price = snapshot.price or quote.price
    high_values = [bar.high for bar in signal.bars if bar.high is not None]
    high_52w = snapshot.fifty_two_week_high or (max(high_values) if high_values else None)
    discount = None
    if high_52w and price:
        discount = (price / high_52w - 1.0) * 100.0
    return (
        f"<b>Tín hiệu lọc sâu VN100: {html.escape(signal.symbol)}</b>\n"
        f"{html.escape(snapshot.name)}\n"
        f"Điểm lọc: <b>{signal.score}/100</b>\n"
        f"Giá: <b>{format_number(price)}</b> VND\n"
        f"P/E: {format_number(snapshot.trailing_pe)} | P/B: {format_number(snapshot.price_to_book)}\n"
        f"Chiết khấu đỉnh 52 tuần: {format_number(discount)}%\n"
        f"Lý do: {html.escape('; '.join(signal.reasons))}\n\n"
        f"<b>Gemini phân tích:</b>\n{html.escape(gemini_text)}\n\n"
        "<i>Tín hiệu nghiên cứu tự động, không phải khuyến nghị mua/bán. Cần tự kiểm tra lại báo cáo tài chính và thanh khoản.</i>"
    )


class DeepSignalScanner:
    """Scan VN100 and emit at most a few high-conviction discount signals."""

    def __init__(
        self,
        provider: YahooQuoteProvider,
        gemini: GeminiAnalyzer,
        symbols: list[str],
        min_score: int = 70,
        max_per_scan: int = 2,
        max_workers: int = DEFAULT_SCAN_WORKERS,
    ):
        self.provider = provider
        self.gemini = gemini
        self.symbols = symbols
        self.min_score = min_score
        self.max_per_scan = max_per_scan
        self.max_workers = max(1, min(int(max_workers), 12))

    @staticmethod
    def _valuation_points(snapshot: FundamentalSnapshot) -> int:
        points = 0
        pe = snapshot.trailing_pe
        pb = snapshot.price_to_book
        if pe is not None:
            if 0 < pe <= 10:
                points += 25
            elif 10 < pe <= 15:
                points += 15
        if pb is not None:
            if 0 < pb <= 1.2:
                points += 25
            elif 1.2 < pb <= 1.8:
                points += 15
        return points

    def _evaluate(
        self,
        symbol: str,
        snapshot: FundamentalSnapshot,
    ) -> DeepSignal | None:
        quote = self.provider.get_quote(symbol)
        bars = self.provider.get_history(symbol, range_value="1y", interval="1d")
        score, reasons = score_candidate(snapshot, quote, bars)
        if score < self.min_score:
            return None
        return DeepSignal(symbol, score, snapshot, quote, bars, reasons)

    def find_candidates(self) -> list[DeepSignal]:
        batch_getter = getattr(self.provider, "get_fundamentals_batch", None)
        if callable(batch_getter):
            try:
                snapshots = batch_getter(self.symbols)
            except BotError as exc:
                LOG.warning("Cannot fetch VN100 fundamentals batch: %s", exc)
                return []
        else:
            snapshots = {}
            for symbol in self.symbols:
                try:
                    snapshots[symbol] = self.provider.get_fundamentals(symbol)
                except BotError as exc:
                    LOG.info("Skipping %s fundamentals: %s", symbol, exc)

        # Price/RSI/discount can contribute at most 60 points. Avoid two Yahoo
        # requests for symbols that cannot mathematically reach the threshold.
        eligible = [
            symbol
            for symbol in self.symbols
            if symbol in snapshots
            and self._valuation_points(snapshots[symbol]) + 60 >= self.min_score
        ]
        candidates: list[DeepSignal] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(eligible) or 1)) as executor:
            future_symbols = {
                executor.submit(self._evaluate, symbol, snapshots[symbol]): symbol
                for symbol in eligible
            }
            for future in as_completed(future_symbols):
                symbol = future_symbols[future]
                try:
                    candidate = future.result()
                except BotError as exc:
                    LOG.info("Skipping %s: %s", symbol, exc)
                    continue
                except Exception as exc:
                    LOG.warning("Unexpected scan error for %s: %s", symbol, type(exc).__name__)
                    continue
                if candidate is not None:
                    candidates.append(candidate)
        return sorted(candidates, key=lambda item: item.score, reverse=True)[: self.max_per_scan]

    def render_signal(self, signal: DeepSignal) -> str:
        return format_deep_signal(signal, self.gemini.analyze(signal))


class TelegramClient:
    """Minimal Bot API client with secret-safe error messages."""

    def __init__(self, token: str, timeout: float = 35.0):
        token = token.strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN chưa được cấu hình.")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload or {}).encode("utf-8")
        request = Request(
            f"{self._base_url}/{method}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "vn-equity-bot/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise BotError("Không kết nối được Telegram.") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BotError("Telegram trả về dữ liệu không hợp lệ.") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            description = result.get("description") if isinstance(result, dict) else None
            # Do not include request URLs or token-bearing details in errors.
            raise BotError(f"Telegram từ chối yêu cầu: {description or 'lỗi không xác định'}.")
        return result

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": max(0, int(timeout)), "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        updates = result.get("result", [])
        return updates if isinstance(updates, list) else []

    def send_message(self, chat_id: int | str, text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )


WELCOME = (
    "<b>VN Equity Bot</b>\n"
    "Tra cứu nhanh dữ liệu tham khảo cho cổ phiếu Việt Nam.\n\n"
    "Gõ /help để xem lệnh. Đây không phải khuyến nghị đầu tư."
)

HELP = (
    "<b>Các lệnh</b>\n"
    "/quote <code>FPT</code> — giá và thay đổi gần nhất\n"
    "/report <code>FPT</code> — báo cáo nhanh: giá, cao/thấp, khối lượng\n"
    "/chart <code>FPT</code> — biểu đồ chữ 30 phiên\n"
    "/ta <code>FPT</code> — MA5, MA20, RSI14, hỗ trợ/kháng cự\n"
    "/deep <code>FPT</code> — phân tích sâu P/E, P/B, chiết khấu\n"
    "/signals_on — nhận tín hiệu lọc sâu VN100\n"
    "/signals_off — tắt tín hiệu lọc sâu\n"
    "/signals_status — trạng thái nhận tín hiệu\n"
    "/scan — quét VN100 ngay\n"
    "/market — VN-Index\n"
    "/add <code>FPT</code> — thêm vào danh sách theo dõi\n"
    "/remove <code>FPT</code> — xóa khỏi danh sách\n"
    "/watchlist — xem danh sách đã lưu\n"
    "/watch — lấy giá toàn bộ danh sách\n"
    "/ping — kiểm tra bot\n\n"
    "Ví dụ: <code>/quote VNM</code>\n"
    "Bạn cũng có thể gõ trực tiếp <code>FPT</code> hoặc <code>VNM</code>."
)


def _argument(text: str) -> str | None:
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else None


def parse_weekdays(raw: str) -> set[int]:
    weekdays: set[int] = set()
    for item in re.split(r"[\s,;]+", raw):
        if not item:
            continue
        try:
            value = int(item)
        except ValueError:
            continue
        if 0 <= value <= 6:
            weekdays.add(value)
    return weekdays or {0, 3}


def parse_scan_time(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
    if not match:
        return 20, 30
    hour = min(max(int(match.group(1)), 0), 23)
    minute = min(max(int(match.group(2)), 0), 59)
    return hour, minute


def parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def build_gemini_analyzer() -> GeminiAnalyzer:
    try:
        max_tokens = int(
            os.environ.get(
                "GEMINI_MAX_OUTPUT_TOKENS",
                str(DEFAULT_GEMINI_MAX_OUTPUT_TOKENS),
            )
        )
        timeout = float(os.environ.get("GEMINI_TIMEOUT", str(DEFAULT_GEMINI_TIMEOUT)))
    except ValueError as exc:
        raise ValueError("GEMINI_MAX_OUTPUT_TOKENS/GEMINI_TIMEOUT phải là số.") from exc
    return GeminiAnalyzer(
        os.environ.get("GEMINI_API_KEY", ""),
        model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        fallback_model=os.environ.get(
            "GEMINI_FALLBACK_MODEL",
            DEFAULT_GEMINI_FALLBACK_MODEL,
        ),
        thinking_level=os.environ.get(
            "GEMINI_THINKING_LEVEL",
            DEFAULT_GEMINI_THINKING_LEVEL,
        ),
        max_output_tokens=max_tokens,
        use_google_search=parse_bool(os.environ.get("GEMINI_GOOGLE_SEARCH"), True),
        timeout=timeout,
    )


def due_for_scheduled_scan(app: "BotApplication", weekdays: set[int], scan_time: tuple[int, int]) -> bool:
    if not app.signal_store:
        return False
    now = datetime.now()
    if now.weekday() not in weekdays:
        return False
    hour, minute = scan_time
    if (now.hour, now.minute) < (hour, minute):
        return False
    today = now.strftime("%Y-%m-%d")
    return app.signal_store.last_scan_date() != today


class BotApplication:
    """Command router separated from the network loop for easy testing."""

    def __init__(
        self,
        telegram: TelegramClient,
        provider: YahooQuoteProvider,
        store: WatchlistStore,
        signal_store: SignalStore | None = None,
        scanner: DeepSignalScanner | None = None,
        monthly_signal_limit: int = DEFAULT_MONTHLY_SIGNAL_LIMIT,
        signal_cooldown_days: int = DEFAULT_SIGNAL_COOLDOWN_DAYS,
    ):
        self.telegram = telegram
        self.provider = provider
        self.store = store
        self.signal_store = signal_store
        self.scanner = scanner
        self.monthly_signal_limit = monthly_signal_limit
        self.signal_cooldown_days = signal_cooldown_days

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(text, str):
            return
        chat_id = chat.get("id")
        if chat_id is None:
            return
        response = self.handle_text(str(text), chat_id)
        if response:
            self.telegram.send_message(chat_id, response)

    def handle_text(self, text: str, chat_id: int | str) -> str | None:
        text = text.strip()
        if not text:
            return None
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        try:
            if command in {"/start", "/help"}:
                return WELCOME if command == "/start" else HELP
            if command == "/ping":
                return "pong ✅"
            if command == "/quote":
                argument = _argument(text)
                if not argument:
                    return "Dùng: /quote <mã>, ví dụ /quote FPT"
                symbol = normalize_symbol(argument.split()[0])
                return format_quote(self.provider.get_quote(symbol))
            if command == "/report":
                argument = _argument(text)
                if not argument:
                    return "Dùng: /report <mã>, ví dụ /report FPT"
                symbol = normalize_symbol(argument.split()[0])
                return format_report(self.provider.get_quote(symbol))
            if command == "/chart":
                argument = _argument(text)
                if not argument:
                    return "Dùng: /chart <mã>, ví dụ /chart FPT"
                symbol = normalize_symbol(argument.split()[0])
                return format_chart(symbol, self.provider.get_history(symbol))
            if command == "/ta":
                argument = _argument(text)
                if not argument:
                    return "Dùng: /ta <mã>, ví dụ /ta FPT"
                symbol = normalize_symbol(argument.split()[0])
                return format_ta(symbol, self.provider.get_history(symbol))
            if command == "/deep":
                argument = _argument(text)
                if not argument:
                    return "Dùng: /deep <mã>, ví dụ /deep FPT"
                symbol = normalize_symbol(argument.split()[0])
                quote = self.provider.get_quote(symbol)
                bars = self.provider.get_history(symbol, range_value="1y", interval="1d")
                snapshot = self.provider.get_fundamentals(symbol)
                score, reasons = score_candidate(snapshot, quote, bars)
                scanner = self.scanner or DeepSignalScanner(
                    self.provider,
                    build_gemini_analyzer(),
                    [symbol],
                )
                return scanner.render_signal(DeepSignal(symbol, score, snapshot, quote, bars, reasons))
            if command == "/signals_on":
                if not self.signal_store:
                    return "Tính năng tín hiệu chưa được cấu hình."
                added = self.signal_store.subscribe(chat_id)
                return "Đã bật tín hiệu lọc sâu VN100." if added else "Bạn đã bật tín hiệu rồi."
            if command == "/signals_off":
                if not self.signal_store:
                    return "Tính năng tín hiệu chưa được cấu hình."
                removed = self.signal_store.unsubscribe(chat_id)
                return "Đã tắt tín hiệu lọc sâu." if removed else "Bạn chưa bật tín hiệu."
            if command == "/signals_status":
                if not self.signal_store:
                    return "Tính năng tín hiệu chưa được cấu hình."
                enabled = str(chat_id) in self.signal_store.chats()
                gemini_status = (
                    self.scanner.gemini.status_text()
                    if self.scanner is not None
                    else "chưa cấu hình"
                )
                return (
                    f"Tín hiệu: {'đang bật' if enabled else 'đang tắt'}\n"
                    f"Gemini: {gemini_status}\n"
                    f"Giới hạn: tối đa {self.monthly_signal_limit} mã/tháng\n"
                    f"Cooldown mỗi mã: {self.signal_cooldown_days} ngày\n"
                    f"Lần quét cuối: {self.signal_store.last_scan_date() or 'chưa có'}"
                )
            if command == "/scan":
                return self.run_signal_scan(manual=True) or "Không có tín hiệu đủ sâu trong lần quét này."
            if command == "/market":
                return format_quote(self.provider.get_quote("VNINDEX"))
            if command == "/add":
                argument = _argument(text)
                if not argument:
                    return "Dùng: /add <mã>, ví dụ /add FPT"
                symbol = normalize_symbol(argument.split()[0])
                return self.store.add(chat_id, symbol)[1]
            if command == "/remove":
                argument = _argument(text)
                if not argument:
                    return "Dùng: /remove <mã>, ví dụ /remove FPT"
                symbol = normalize_symbol(argument.split()[0])
                return self.store.remove(chat_id, symbol)[1]
            if command == "/watchlist":
                symbols = self.store.get(chat_id)
                return (
                    "Danh sách theo dõi đang trống. Dùng /add <mã> để thêm."
                    if not symbols
                    else "Danh sách theo dõi: " + ", ".join(f"<code>{s}</code>" for s in symbols)
                )
            if command == "/watch":
                symbols = self.store.get(chat_id)
                if not symbols:
                    return "Danh sách theo dõi đang trống. Dùng /add <mã> để thêm."
                lines = ["<b>Theo dõi</b>"]
                for symbol in symbols:
                    try:
                        lines.append(format_quote(self.provider.get_quote(symbol)))
                    except BotError as exc:
                        lines.append(f"<b>{html.escape(symbol)}</b>: {html.escape(str(exc))}")
                return "\n\n".join(lines)
            if command.startswith("/"):
                return "Chưa nhận diện lệnh. Gõ /help để xem hướng dẫn."
            if SYMBOL_RE.fullmatch(text.upper()):
                symbol = normalize_symbol(text)
                return format_quote(self.provider.get_quote(symbol))
            return None
        except ValueError as exc:
            return html.escape(str(exc))
        except BotError as exc:
            return html.escape(str(exc))

    def run_signal_scan(self, manual: bool = False) -> str | None:
        if not self.signal_store or not self.scanner:
            return "Tính năng tín hiệu chưa được cấu hình."
        chats = self.signal_store.chats()
        if not chats and not manual:
            return None
        now = datetime.now()
        remaining = self.monthly_signal_limit - self.signal_store.sent_this_month(now)
        if remaining <= 0:
            return "Tháng này đã đủ số tín hiệu theo giới hạn."
        candidates = self.scanner.find_candidates()
        messages: list[str] = []
        for candidate in candidates:
            if len(messages) >= remaining:
                break
            if self.signal_store.recently_sent(candidate.symbol, now, self.signal_cooldown_days):
                continue
            messages.append(self.scanner.render_signal(candidate))
            self.signal_store.record_sent(candidate.symbol, now)
        if manual and not messages:
            return "Quét xong: chưa có mã VN100 nào đạt ngưỡng chiết khấu đủ sâu."
        for message in messages:
            for chat_id in chats:
                try:
                    self.telegram.send_message(chat_id, message)
                except BotError as exc:
                    LOG.warning("Cannot send signal to %s: %s", chat_id, exc)
        if manual:
            return "\n\n".join(messages)
        return None


def run_polling(
    app: BotApplication,
    poll_timeout: int = DEFAULT_POLL_TIMEOUT,
    scan_weekdays: set[int] | None = None,
    scan_time: tuple[int, int] | None = None,
) -> None:
    """Run long polling until interrupted, backing off on transient errors."""

    app.telegram.delete_webhook()
    offset: int | None = None
    backoff = 1.0
    scan_weekdays = scan_weekdays or parse_weekdays(DEFAULT_SCAN_WEEKDAYS)
    scan_time = scan_time or parse_scan_time(DEFAULT_SCAN_TIME)
    LOG.info("Bot polling started.")
    while True:
        try:
            if due_for_scheduled_scan(app, scan_weekdays, scan_time):
                today = datetime.now().strftime("%Y-%m-%d")
                LOG.info("Scheduled VN100 signal scan started.")
                app.run_signal_scan(manual=False)
                if app.signal_store:
                    app.signal_store.mark_scan(today)
            updates = app.telegram.get_updates(offset, poll_timeout)
            backoff = 1.0
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                try:
                    app.handle_update(update)
                except BotError as exc:
                    LOG.warning("Update handling failed: %s", exc)
        except KeyboardInterrupt:
            LOG.info("Stopping bot.")
            return
        except BotError as exc:
            LOG.warning("Polling error: %s; retrying in %.1fs", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def build_application() -> BotApplication:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    try:
        poll_timeout = float(os.environ.get("POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT))
        yahoo_timeout = float(os.environ.get("YAHOO_TIMEOUT", DEFAULT_YAHOO_TIMEOUT))
        min_signal_score = int(os.environ.get("MIN_SIGNAL_SCORE", "70"))
        max_signals_per_scan = int(os.environ.get("MAX_SIGNALS_PER_SCAN", "2"))
        monthly_signal_limit = int(os.environ.get("MONTHLY_SIGNAL_LIMIT", DEFAULT_MONTHLY_SIGNAL_LIMIT))
        signal_cooldown_days = int(os.environ.get("SIGNAL_COOLDOWN_DAYS", DEFAULT_SIGNAL_COOLDOWN_DAYS))
        scan_workers = int(os.environ.get("SCAN_WORKERS", DEFAULT_SCAN_WORKERS))
    except ValueError as exc:
        raise ValueError("Các biến timeout/score/limit phải là số.") from exc
    telegram = TelegramClient(token, timeout=max(10.0, poll_timeout + 10.0))
    provider = YahooQuoteProvider(timeout=max(1.0, yahoo_timeout))
    gemini = build_gemini_analyzer()
    symbols = parse_symbols(os.environ.get("VN100_SYMBOLS"))
    return BotApplication(
        telegram=telegram,
        provider=provider,
        store=WatchlistStore(data_dir / "watchlists.json"),
        signal_store=SignalStore(data_dir / "signal_state.json"),
        scanner=DeepSignalScanner(
            provider=provider,
            gemini=gemini,
            symbols=symbols,
            min_score=min_signal_score,
            max_per_scan=max_signals_per_scan,
            max_workers=scan_workers,
        ),
        monthly_signal_limit=monthly_signal_limit,
        signal_cooldown_days=signal_cooldown_days,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the VN Equity Telegram bot.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one getUpdates request then exit (smoke testing).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    try:
        app = build_application()
        if args.once:
            app.telegram.delete_webhook()
            for update in app.telegram.get_updates(None, 0):
                app.handle_update(update)
            return 0
        run_polling(
            app,
            int(os.environ.get("POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT)),
            scan_weekdays=parse_weekdays(os.environ.get("SCAN_WEEKDAYS", DEFAULT_SCAN_WEEKDAYS)),
            scan_time=parse_scan_time(os.environ.get("SCAN_TIME", DEFAULT_SCAN_TIME)),
        )
        return 0
    except (ValueError, BotError) as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
