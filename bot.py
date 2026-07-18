"""Telegram bot for quick Vietnamese equity lookups.

The project intentionally uses only Python's standard library.  The Telegram
token is read from TELEGRAM_BOT_TOKEN and is never accepted on the command
line or written to disk.

Commands:
    /start
    /help
    /quote <TICKER>
    /report <TICKER>
    /chart <TICKER>
    /ta <TICKER>
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote as url_quote
from urllib.request import Request, urlopen


LOG = logging.getLogger("vn_equity_bot")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}$")
MAX_WATCHLIST_SIZE = 10
DEFAULT_POLL_TIMEOUT = 25
DEFAULT_YAHOO_TIMEOUT = 12


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


class YahooQuoteProvider:
    """Small, cache-aware Yahoo Finance chart client."""

    def __init__(self, timeout: float = DEFAULT_YAHOO_TIMEOUT, cache_ttl: float = 60.0):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._history_cache: dict[tuple[str, str, str], tuple[float, list[PriceBar]]] = {}

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


class BotApplication:
    """Command router separated from the network loop for easy testing."""

    def __init__(
        self,
        telegram: TelegramClient,
        provider: YahooQuoteProvider,
        store: WatchlistStore,
    ):
        self.telegram = telegram
        self.provider = provider
        self.store = store

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


def run_polling(app: BotApplication, poll_timeout: int = DEFAULT_POLL_TIMEOUT) -> None:
    """Run long polling until interrupted, backing off on transient errors."""

    app.telegram.delete_webhook()
    offset: int | None = None
    backoff = 1.0
    LOG.info("Bot polling started.")
    while True:
        try:
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
    except ValueError as exc:
        raise ValueError("POLL_TIMEOUT/YAHOO_TIMEOUT phải là số.") from exc
    telegram = TelegramClient(token, timeout=max(10.0, poll_timeout + 10.0))
    return BotApplication(
        telegram=telegram,
        provider=YahooQuoteProvider(timeout=max(1.0, yahoo_timeout)),
        store=WatchlistStore(data_dir / "watchlists.json"),
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
        run_polling(app, int(os.environ.get("POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT)))
        return 0
    except (ValueError, BotError) as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
