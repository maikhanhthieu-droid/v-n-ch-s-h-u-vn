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
    /news <TICKER or TOPIC>
    /macro [TOPIC]
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
import math
import os
import re
import signal
import sys
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
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
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-2.5-flash-lite"
DEFAULT_GEMINI_THINKING_LEVEL = "minimal"
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 1000
DEFAULT_GEMINI_TIMEOUT = 30
DEFAULT_GEMINI_MIN_INTERVAL = 60.0
DEFAULT_GEMINI_CACHE_TTL = 1800
DEFAULT_GEMINI_QUOTA_COOLDOWN = 900
DEFAULT_RESEARCH_COMMAND_COOLDOWN = 60.0
DEFAULT_GEMINI_DAILY_BUDGET = 12
DEFAULT_DEEP_DAILY_LIMIT = 10
DEFAULT_SCAN_DAILY_LIMIT = 2
DEFAULT_NEWS_DAILY_LIMIT = 8
DEFAULT_SCAN_WORKERS = 2
DEFAULT_VNSTOCK_DAILY_BUDGET = 60
DEFAULT_VNSTOCK_REQUESTS_PER_MINUTE = 12
DEFAULT_VNSTOCK_USAGE_RATIO = 0.70
DEFAULT_VNSTOCK_ERROR_COOLDOWN = 300.0
DEFAULT_VNSTOCK_CACHE_TTL = 480.0
DEFAULT_VNSTOCK_SOURCES = "VCI,KBS"
DEFAULT_VIMO_LATEST_URL = (
    "https://raw.githubusercontent.com/maikhanhthieu-droid/"
    "vimo-VN/main/output/latest.json"
)
DEFAULT_VIMO_CACHE_TTL = 900.0
DEFAULT_VIMO_MAX_AGE_HOURS = 72.0
DEFAULT_NEWS_CACHE_TTL = 900.0
DEFAULT_NEWS_MAX_ITEMS = 5

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


class GeminiQuotaCircuitOpen(RuntimeError):
    """Raised internally while Gemini calls are paused after a quota error."""


class GeminiRequestCooldown(RuntimeError):
    """Raised internally instead of blocking Telegram during rate limiting."""

    def __init__(self, remaining: float):
        self.remaining = max(1, int(remaining) + 1)
        super().__init__(f"Gemini request cooldown: {self.remaining}s")


class GeminiDailyBudgetReached(RuntimeError):
    """Raised internally before an API call would exceed the daily budget."""


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
    """Business, valuation, and price fields used by the VN100 scanner."""

    symbol: str
    name: str
    price: float | None = None
    trailing_pe: float | None = None
    price_to_book: float | None = None
    market_cap: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    sector: str | None = None
    industry: str | None = None
    total_revenue: float | None = None
    revenue_growth: float | None = None
    net_income: float | None = None
    net_income_growth: float | None = None
    return_on_equity: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    earnings_per_share: float | None = None
    earnings_growth: float | None = None


@dataclass(frozen=True)
class MacroSource:
    """A traceable source inherited from the vimo-VN macro repository."""

    title: str
    url: str
    as_of: str = ""
    quality: str = ""


@dataclass(frozen=True)
class MacroContext:
    """Neutral macro state produced by vimo-VN."""

    stance: str = "Chưa đủ dữ liệu"
    score: float | None = None
    summary: str = "Chưa tải được bối cảnh vĩ mô."
    positive_drivers: tuple[str, ...] = ()
    risk_drivers: tuple[str, ...] = ()
    neutral_drivers: tuple[str, ...] = ()
    policy_notes: tuple[str, ...] = ()
    generated_at: str = ""
    confidence: str = ""
    sources: tuple[MacroSource, ...] = ()
    available: bool = False
    age_hours: float | None = None


@dataclass(frozen=True)
class ScoreBreakdown:
    """Deterministic 100-point framework. Gemini may explain but not edit it."""

    business: int
    valuation: int
    technical: int
    risk: int
    macro: int
    total: int
    pattern: str


@dataclass(frozen=True)
class TradePlan:
    """Scenario levels derived from volatility; not a personalized order."""

    entry_low: float
    entry_high: float
    stop: float
    target_1: float
    target_2: float
    target_3: float
    risk_pct: float
    support: float | None = None
    resistance: float | None = None


@dataclass(frozen=True)
class BacktestResult:
    """Historical pattern outcomes. This is not a forecast probability."""

    samples: int
    wins: int
    losses: int
    unresolved: int
    hit_rate: float | None
    positive_closes: int = 0
    positive_close_rate: float | None = None
    median_return: float | None = None
    lookahead_sessions: int = 20
    target_label: str = "T1"


@dataclass(frozen=True)
class NewsItem:
    """Headline metadata from a public RSS feed."""

    title: str
    source: str
    url: str
    published_at: str = ""


@dataclass(frozen=True)
class DeepSignal:
    """A candidate with deterministic scores and scenario statistics."""

    symbol: str
    score: int
    snapshot: FundamentalSnapshot
    quote: Quote
    bars: list[PriceBar]
    reasons: list[str]
    breakdown: ScoreBreakdown | None = None
    macro: MacroContext | None = None
    trade_plan: TradePlan | None = None
    backtest: BacktestResult | None = None
    backtest_3m: BacktestResult | None = None


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


def _text_request(url: str, timeout: float, accept: str = "*/*") -> str:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "vn-equity-bot/1.0 (+https://github.com/)",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise BotError("Không tải được nguồn tin công khai lúc này.") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BotError("Nguồn tin trả về định dạng không hợp lệ.") from exc


def _string_items(value: Any, limit: int = 6) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(
                item.get("text")
                or item.get("reason")
                or item.get("name")
                or item.get("label")
                or ""
            ).strip()
        else:
            text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return tuple(items)


def _fold_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks.casefold()).strip()


def _company_search_name(value: str) -> str:
    cleaned = re.sub(
        r"\b(jsc|corp(?:oration)?|company|joint stock|holdings?|group|plc|ltd)\b\.?",
        " ",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" -,.")


class MacroContextClient:
    """Read the latest neutral macro snapshot published by vimo-VN."""

    RELEVANT_CARDS = {
        "cpi",
        "pmi",
        "iip",
        "retail_sales",
        "interbank",
        "usd_vnd",
        "vnindex",
        "dxy",
        "oil",
        "us10y",
        "policy_actions_vn",
        "trade_balance",
    }

    def __init__(
        self,
        url: str = DEFAULT_VIMO_LATEST_URL,
        timeout: float = DEFAULT_YAHOO_TIMEOUT,
        cache_ttl: float = DEFAULT_VIMO_CACHE_TTL,
        max_age_hours: float = DEFAULT_VIMO_MAX_AGE_HOURS,
        fetcher: Callable[[str, float], dict[str, Any]] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.url = url.strip() or DEFAULT_VIMO_LATEST_URL
        self.timeout = max(2.0, float(timeout))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.max_age_hours = max(1.0, float(max_age_hours))
        self.fetcher = fetcher or _json_request
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._cache: tuple[float, MacroContext] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _unavailable(summary: str) -> MacroContext:
        return MacroContext(summary=summary, available=False)

    @classmethod
    def parse(
        cls,
        payload: dict[str, Any],
        *,
        max_age_hours: float = DEFAULT_VIMO_MAX_AGE_HOURS,
        now: datetime | None = None,
    ) -> MacroContext:
        strategy = payload.get("macro_strategy")
        if not isinstance(strategy, dict):
            return cls._unavailable("vimo-VN chưa xuất bản phần macro_strategy.")

        score = _first_number(strategy.get("score"))
        stance = str(strategy.get("stance") or "Chưa đủ dữ liệu").strip()
        summary = str(
            strategy.get("reason_short")
            or strategy.get("summary")
            or "Chưa có nhận định tóm tắt."
        ).strip()
        positive = _string_items(strategy.get("positive_drivers"))
        risks = _string_items(strategy.get("risk_drivers"))
        neutral = _string_items(strategy.get("neutral_drivers"))
        confidence = str(strategy.get("confidence") or "").strip()
        generated_at = str(
            payload.get("generated_at_bkk")
            or payload.get("generated_at")
            or ""
        ).strip()
        if not generated_at:
            return cls._unavailable(
                "vimo-VN thiếu generated_at; điểm vĩ mô được giữ ở mức trung tính."
            )
        try:
            generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            reference_now = now or datetime.now(timezone.utc)
            if reference_now.tzinfo is None:
                reference_now = reference_now.replace(tzinfo=timezone.utc)
            age_hours = max(
                0.0,
                (reference_now.astimezone(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds()
                / 3600,
            )
        except ValueError:
            return cls._unavailable(
                "vimo-VN có generated_at không hợp lệ; điểm vĩ mô được giữ ở mức trung tính."
            )
        if age_hours > max_age_hours:
            return MacroContext(
                summary=(
                    f"vimo-VN đã quá hạn ({age_hours:.1f} giờ); "
                    "điểm vĩ mô được giữ ở mức trung tính."
                ),
                generated_at=generated_at,
                available=False,
                age_hours=round(age_hours, 2),
            )

        cards_raw = payload.get("cards")
        if isinstance(cards_raw, dict):
            cards = list(cards_raw.values())
        elif isinstance(cards_raw, list):
            cards = cards_raw
        else:
            cards = []
        sources: list[MacroSource] = []
        policy_notes: list[str] = []
        seen_urls: set[str] = set()
        for card in cards:
            if not isinstance(card, dict):
                continue
            key = str(card.get("key") or "").strip().lower()
            if key not in cls.RELEVANT_CARDS:
                continue
            if key == "policy_actions_vn":
                value = str(card.get("value") or "").strip()
                narrative = str(card.get("narrative") or "").strip()
                note = " — ".join(part for part in (value, narrative) if part)
                if note:
                    policy_notes.append(note[:600])
            url = str(card.get("source_url") or "").strip()
            if not HTTP_URL_RE.match(url) or url in seen_urls:
                continue
            title = str(
                card.get("source_primary")
                or card.get("name_vi")
                or key
            ).strip()
            sources.append(
                MacroSource(
                    title=title[:90],
                    url=url,
                    as_of=str(card.get("as_of") or "").strip(),
                    quality=str(card.get("source_quality") or "").strip(),
                )
            )
            seen_urls.add(url)
            if len(sources) >= 6:
                break
        return MacroContext(
            stance=stance,
            score=score,
            summary=summary,
            positive_drivers=positive,
            risk_drivers=risks,
            neutral_drivers=neutral,
            policy_notes=tuple(policy_notes[:3]),
            generated_at=generated_at,
            confidence=confidence,
            sources=tuple(sources),
            available=True,
            age_hours=round(age_hours, 2),
        )

    def latest(self) -> MacroContext:
        now = time.monotonic()
        with self._lock:
            if self._cache and self._cache[0] > now:
                return self._cache[1]
        try:
            context = self.parse(
                self.fetcher(self.url, self.timeout),
                max_age_hours=self.max_age_hours,
                now=self.now_factory(),
            )
        except (BotError, TypeError, ValueError) as exc:
            LOG.warning("Cannot load vimo-VN macro context: %s", type(exc).__name__)
            context = self._unavailable(
                "Tạm thời chưa tải được vimo-VN; điểm vĩ mô được giữ ở mức trung tính."
            )
        with self._lock:
            self._cache = (now + self.cache_ttl, context)
        return context


class NeutralNewsService:
    """Fetch public Vietnamese headlines; interpretation remains source-bound."""

    def __init__(
        self,
        timeout: float = DEFAULT_YAHOO_TIMEOUT,
        cache_ttl: float = DEFAULT_NEWS_CACHE_TTL,
        max_items: int = DEFAULT_NEWS_MAX_ITEMS,
        fetcher: Callable[[str, float, str], str] | None = None,
    ):
        self.timeout = max(2.0, float(timeout))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.max_items = max(1, min(int(max_items), 10))
        self.fetcher = fetcher or _text_request
        self._cache: dict[str, tuple[float, list[NewsItem]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _parse_rss(raw: str, max_items: int) -> list[NewsItem]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise BotError("Nguồn RSS tin tức trả về định dạng không hợp lệ.") from exc
        results: list[NewsItem] = []
        seen_titles: set[str] = set()
        for item in root.findall(".//item"):
            title_node = item.find("title")
            link_node = item.find("link")
            source_node = item.find("source")
            date_node = item.find("pubDate")
            title = html.unescape(
                "".join(title_node.itertext()) if title_node is not None else ""
            ).strip()
            url = (
                "".join(link_node.itertext()).strip()
                if link_node is not None
                else ""
            )
            source = (
                html.unescape("".join(source_node.itertext())).strip()
                if source_node is not None
                else "Nguồn RSS"
            )
            published_at = (
                "".join(date_node.itertext()).strip()
                if date_node is not None
                else ""
            )
            normalized_title = re.sub(r"\s+", " ", title)
            if (
                not normalized_title
                or normalized_title.lower() in seen_titles
                or not HTTP_URL_RE.match(url)
            ):
                continue
            seen_titles.add(normalized_title.lower())
            results.append(
                NewsItem(
                    title=normalized_title[:240],
                    source=source[:80],
                    url=url,
                    published_at=published_at[:80],
                )
            )
            if len(results) >= max_items:
                break
        return results

    @staticmethod
    def _matches_terms(title: str, terms: Iterable[str]) -> bool:
        folded_title = _fold_search_text(title)
        padded_title = f" {folded_title} "
        for term in terms:
            folded_term = _fold_search_text(term)
            if not folded_term:
                continue
            if " " in folded_term:
                if folded_term in folded_title:
                    return True
            elif f" {folded_term} " in padded_title:
                return True
        return False

    def headlines(
        self,
        topic: str,
        required_terms: Iterable[str] | None = None,
    ) -> list[NewsItem]:
        clean_topic = re.sub(r"\s+", " ", topic).strip()[:120]
        if not clean_topic:
            return []
        terms = tuple(
            str(term).strip()
            for term in (required_terms or ())
            if str(term).strip()
        )
        cache_key = f"{clean_topic.casefold()}|{'|'.join(term.casefold() for term in terms)}"
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                return list(cached[1])
        query = f"{clean_topic} (doanh nghiệp OR kinh tế OR chứng khoán OR thông tư)"
        url = (
            "https://news.google.com/rss/search?"
            f"q={url_quote(query)}&hl=vi&gl=VN&ceid=VN:vi"
        )
        raw = self.fetcher(url, self.timeout, "application/rss+xml, application/xml")
        results = self._parse_rss(raw, self.max_items)
        if terms:
            results = [item for item in results if self._matches_terms(item.title, terms)]
        with self._lock:
            self._cache[cache_key] = (now + self.cache_ttl, list(results))
        return results

    def stock_headlines(self, symbol: str, company_name: str) -> list[NewsItem]:
        clean_symbol = normalize_symbol(symbol)
        clean_company = _company_search_name(company_name)
        aliases = [clean_symbol]
        if clean_company:
            aliases.append(clean_company)
        quoted = " OR ".join(f'"{alias}"' for alias in aliases)
        query = f"({quoted}) (cổ phiếu OR doanh nghiệp)"
        return self.headlines(query, required_terms=aliases)


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
                    "sector",
                    "industry",
                    "total_revenue",
                    "total_revenue_yoy_growth_ttm",
                    "net_income",
                    "net_income_yoy_growth_ttm",
                    "return_on_equity_fq",
                    "debt_to_equity_fq",
                    "current_ratio_fq",
                    "earnings_per_share_diluted_ttm",
                    "earnings_per_share_diluted_yoy_growth_ttm",
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
                sector=str(values[6]).strip() if len(values) > 6 and values[6] else None,
                industry=str(values[7]).strip() if len(values) > 7 and values[7] else None,
                total_revenue=_first_number(values[8]) if len(values) > 8 else None,
                revenue_growth=_first_number(values[9]) if len(values) > 9 else None,
                net_income=_first_number(values[10]) if len(values) > 10 else None,
                net_income_growth=_first_number(values[11]) if len(values) > 11 else None,
                return_on_equity=_first_number(values[12]) if len(values) > 12 else None,
                debt_to_equity=_first_number(values[13]) if len(values) > 13 else None,
                current_ratio=_first_number(values[14]) if len(values) > 14 else None,
                earnings_per_share=_first_number(values[15]) if len(values) > 15 else None,
                earnings_growth=_first_number(values[16]) if len(values) > 16 else None,
            )
            snapshots[ticker] = snapshot
            self._fundamental_cache[ticker] = (now, snapshot)
        return snapshots


def _is_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "429",
            "too many request",
            "rate limit",
            "ratelimit",
            "quota",
            "exceeded",
            "temporarily blocked",
        )
    )


def _is_unsupported_source_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "provider 'quote/" in text
        or ("available:" in text and "quote" in text)
        or ("source" in text and ("unsupported" in text or "not support" in text))
    )


def _history_window(range_value: str) -> tuple[str, str]:
    """Translate Yahoo-style ranges into vnstock start/end dates."""

    normalized = str(range_value or "6mo").strip().lower()
    days_by_range = {
        "1mo": 45,
        "3mo": 120,
        "6mo": 220,
        "1y": 380,
        "2y": 760,
        "5y": 1900,
    }
    days = days_by_range.get(normalized, 220)
    now = datetime.now()
    end = now.strftime("%Y-%m-%d")
    start = datetime.fromtimestamp(now.timestamp() - days * 86400).strftime("%Y-%m-%d")
    return start, end


def _vnstock_bars(raw: Any, symbol: str) -> list[PriceBar]:
    """Normalize a vnstock DataFrame without binding the bot to pandas APIs."""

    if raw is None:
        return []
    try:
        records = raw.to_dict("records") if hasattr(raw, "to_dict") else list(raw)
    except (TypeError, ValueError):
        return []
    parsed: list[dict[str, float | int | None]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        row = {str(key).lower(): value for key, value in item.items()}
        def finite_number(*values: Any) -> float | None:
            value = _first_number(*values)
            return value if value is not None and math.isfinite(value) else None

        close = finite_number(row.get("close"), row.get("c"))
        if close is None:
            continue
        volume = finite_number(row.get("volume"), row.get("v"))
        parsed.append(
            {
                "close": close,
                "high": finite_number(row.get("high"), row.get("h")),
                "low": finite_number(row.get("low"), row.get("l")),
                "open": finite_number(row.get("open"), row.get("o")),
                "volume": int(volume) if volume is not None else None,
            }
        )
    if len(parsed) < 2:
        return []

    # vnstock commonly reports Vietnamese shares in thousands of VND while
    # Yahoo/TradingView report VND. Keep one unit throughout the bot.
    is_index = symbol.upper() in {"VNINDEX", "VN30", "HNXINDEX", "UPCOMINDEX"}
    scale = 1.0
    if not is_index and max(abs(float(row["close"])) for row in parsed) < 10_000:
        scale = 1000.0

    def scaled(value: float | int | None) -> float | None:
        return float(value) * scale if value is not None else None

    return [
        PriceBar(
            close=float(row["close"]) * scale,
            high=scaled(row["high"]),
            low=scaled(row["low"]),
            open_price=scaled(row["open"]),
            volume=int(row["volume"]) if row["volume"] is not None else None,
        )
        for row in parsed
    ]


class VnstockHistoryClient:
    """Thin compatibility layer for vnstock import-path changes."""

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        if self.api_key:
            os.environ.setdefault("VNSTOCK_API_KEY", self.api_key)
            os.environ.setdefault("VNDATA_API_KEY", self.api_key)
        try:
            from vnstock.api.quote import Quote as vn_quote
        except ImportError:
            try:
                from vnstock import Quote as vn_quote
            except ImportError as exc:
                raise RuntimeError("Thư viện vnstock chưa được cài đặt.") from exc
        self._quote_class = vn_quote

    def fetch(
        self,
        source: str,
        symbol: str,
        range_value: str,
        interval: str,
    ) -> list[PriceBar]:
        start, end = _history_window(range_value)
        vn_interval = "1D" if interval.lower() in {"1d", "d", "day"} else interval
        quote = self._quote_class(symbol=symbol.upper(), source=source.lower())
        raw = quote.history(start=start, end=end, interval=vn_interval)
        bars = _vnstock_bars(raw, symbol)
        if len(bars) < 2:
            raise ValueError(f"vnstock {source} returned insufficient OHLCV for {symbol}")
        return bars


class VnstockSourceGate:
    """Per-source throttle and circuit breaker inspired by THIUCUBU."""

    def __init__(
        self,
        source: str,
        requests_per_minute: int,
        usage_ratio: float,
        error_cooldown: float,
    ):
        self.source = source
        self.rpm_limit = max(1, int(requests_per_minute))
        self.usage_ratio = max(0.05, min(1.0, float(usage_ratio)))
        self.effective_rpm = self.rpm_limit * self.usage_ratio
        self.min_interval = 60.0 / self.effective_rpm
        self.error_cooldown = max(30.0, float(error_cooldown))
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.rate_limit_failures = 0
        self.disabled = False
        self.last_error = ""
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        with self._lock:
            return not self.disabled and time.monotonic() >= self.cooldown_until

    def wait_turn(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self.next_at - now)
            reserved_at = max(now, self.next_at)
            self.next_at = reserved_at + self.min_interval
            self.attempts += 1
        if wait_for > 0:
            time.sleep(wait_for)

    def record_success(self) -> None:
        with self._lock:
            self.successes += 1
            self.failures = 0
            self.last_error = ""

    def record_failure(self, exc: BaseException) -> None:
        with self._lock:
            self.failures += 1
            self.last_error = str(exc).splitlines()[0][:120]
            if _is_unsupported_source_error(exc):
                self.disabled = True
                return
            if _is_rate_limit_error(exc):
                self.rate_limit_failures += 1
                self.cooldown_until = max(
                    self.cooldown_until,
                    time.monotonic() + self.error_cooldown,
                )
            elif self.failures >= 3:
                self.cooldown_until = max(
                    self.cooldown_until,
                    time.monotonic() + min(120.0, self.error_cooldown),
                )
                self.failures = 0

    def health(self) -> dict[str, Any]:
        with self._lock:
            attempts = self.attempts
            success_rate = 1.0 if attempts == 0 else self.successes / attempts
            penalty = self.rate_limit_failures * 15
            score = max(0, min(100, round(success_rate * 100 - penalty)))
            cooling = max(0, round(self.cooldown_until - time.monotonic()))
            return {
                "source": self.source,
                "effective_rpm": self.effective_rpm,
                "attempts": attempts,
                "successes": self.successes,
                "score": score,
                "cooling": cooling,
                "disabled": self.disabled,
            }


class RoutedMarketDataProvider:
    """Route OHLCV across vnstock sources, with Yahoo as a safe fallback."""

    SUPPORTED_SOURCES = ("VCI", "KBS")

    def __init__(
        self,
        fallback: YahooQuoteProvider,
        usage_store: "ApiUsageStore",
        api_key: str,
        sources: Iterable[str] = ("VCI", "KBS"),
        requests_per_minute: int = DEFAULT_VNSTOCK_REQUESTS_PER_MINUTE,
        usage_ratio: float = DEFAULT_VNSTOCK_USAGE_RATIO,
        error_cooldown: float = DEFAULT_VNSTOCK_ERROR_COOLDOWN,
        cache_ttl: float = DEFAULT_VNSTOCK_CACHE_TTL,
        history_fetcher: Callable[[str, str, str, str], list[PriceBar]] | None = None,
    ):
        self.fallback = fallback
        self.usage_store = usage_store
        self.api_key = api_key.strip()
        normalized_sources = [
            str(source).strip().upper()
            for source in sources
            if str(source).strip().upper() in self.SUPPORTED_SOURCES
        ]
        self.sources = list(dict.fromkeys(normalized_sources)) or list(self.SUPPORTED_SOURCES)
        self.cache_ttl = max(0.0, float(cache_ttl))
        self._history_cache: dict[tuple[str, str, str], tuple[float, list[PriceBar]]] = {}
        self._unavailable_reason = ""
        self._fetch_history = history_fetcher
        if self._fetch_history is None and self.api_key:
            try:
                self._fetch_history = VnstockHistoryClient(self.api_key).fetch
            except (Exception, SystemExit) as exc:
                self._unavailable_reason = str(exc)
                LOG.warning("VNStock disabled: %s", exc)
        elif not self.api_key:
            self._unavailable_reason = "chưa có VNSTOCK_API_KEY"
        self._gates = {
            source: VnstockSourceGate(
                source,
                requests_per_minute=requests_per_minute,
                usage_ratio=usage_ratio,
                error_cooldown=error_cooldown,
            )
            for source in self.sources
        }

    def get_quote(self, symbol: str) -> Quote:
        # Yahoo is faster for the latest quote; VNStock is reserved for OHLCV.
        return self.fallback.get_quote(symbol)

    def get_fundamentals(self, symbol: str) -> FundamentalSnapshot:
        return self.fallback.get_fundamentals(symbol)

    def get_fundamentals_batch(
        self,
        symbols: Iterable[str],
    ) -> dict[str, FundamentalSnapshot]:
        return self.fallback.get_fundamentals_batch(symbols)

    def _source_order(self, symbol: str) -> list[str]:
        start = sum(ord(char) for char in symbol.upper()) % len(self.sources)
        return self.sources[start:] + self.sources[:start]

    def get_history(
        self,
        symbol: str,
        range_value: str = "6mo",
        interval: str = "1d",
    ) -> list[PriceBar]:
        normalized = symbol.upper()
        cache_key = (normalized, range_value, interval)
        cached = self._history_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        if self._fetch_history is not None:
            for source in self._source_order(normalized):
                gate = self._gates[source]
                if not gate.is_available():
                    continue
                allowed, _, _ = self.usage_store.claim("vnstock_requests")
                if not allowed:
                    break
                gate.wait_turn()
                try:
                    bars = self._fetch_history(source, normalized, range_value, interval)
                    if len(bars) < 2:
                        raise ValueError(f"{source} returned insufficient OHLCV")
                except SystemExit as exc:
                    gate.record_failure(exc)
                    LOG.warning("[%s] VNStock stopped for %s: %s", source, normalized, exc)
                    continue
                except Exception as exc:
                    gate.record_failure(exc)
                    LOG.warning("[%s] VNStock failed for %s: %s", source, normalized, exc)
                    continue
                gate.record_success()
                self._history_cache[cache_key] = (now, bars)
                return bars

        bars = self.fallback.get_history(normalized, range_value, interval)
        self._history_cache[cache_key] = (now, bars)
        return bars

    def status_text(self) -> str:
        lines = [
            "<b>Luồng dữ liệu thị trường</b>",
            "Giá nhanh: Yahoo",
            f"Lịch sử/TA: {' ↔ '.join(self.sources)} → Yahoo dự phòng",
            "Định giá: TradingView theo lô",
        ]
        if self._unavailable_reason:
            lines.append(f"VNStock: tạm không dùng ({html.escape(self._unavailable_reason)})")
        for source in self.sources:
            state = self._gates[source].health()
            if state["disabled"]:
                status = "đã tắt"
            elif state["cooling"]:
                status = f"nghỉ {state['cooling']} giây"
            else:
                status = "sẵn sàng"
            lines.append(
                f"{source}: khỏe {state['score']}% — "
                f"{state['successes']}/{state['attempts']} thành công, "
                f"{state['effective_rpm']:.1f} lần/phút, {status}"
            )
        return "\n".join(lines)


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


class ApiUsageStore:
    """Persist conservative daily counters used to protect upstream quotas."""

    COUNTERS = (
        "gemini_requests",
        "vnstock_requests",
        "deep_commands",
        "scan_commands",
        "news_commands",
    )

    def __init__(
        self,
        path: Path,
        gemini_daily_budget: int = DEFAULT_GEMINI_DAILY_BUDGET,
        vnstock_daily_budget: int = DEFAULT_VNSTOCK_DAILY_BUDGET,
        deep_daily_limit: int = DEFAULT_DEEP_DAILY_LIMIT,
        scan_daily_limit: int = DEFAULT_SCAN_DAILY_LIMIT,
        news_daily_limit: int = DEFAULT_NEWS_DAILY_LIMIT,
    ):
        self.path = path
        self.limits = {
            "gemini_requests": max(1, int(gemini_daily_budget)),
            "vnstock_requests": max(1, int(vnstock_daily_budget)),
            "deep_commands": max(1, int(deep_daily_limit)),
            "scan_commands": max(1, int(scan_daily_limit)),
            "news_commands": max(1, int(news_daily_limit)),
        }
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._empty_data()
        self._load()

    @staticmethod
    def _today() -> str:
        # Fixed UTC+7 keeps the daily reset stable on Windows and cloud hosts.
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 7 * 3600))

    def _empty_data(self) -> dict[str, Any]:
        return {"date": self._today(), **{name: 0 for name in self.COUNTERS}}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("API usage file is unreadable; starting empty.")
            return
        if not isinstance(raw, dict):
            return
        self._data = {
            "date": str(raw.get("date") or ""),
            **{
                name: max(0, int(raw.get(name, 0)))
                if str(raw.get(name, 0)).lstrip("-").isdigit()
                else 0
                for name in self.COUNTERS
            },
        }
        self._rollover_locked()

    def _rollover_locked(self) -> None:
        if self._data.get("date") != self._today():
            self._data = self._empty_data()
            self._save_locked()

    def _save_locked(self) -> None:
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

    def claim(self, counter: str) -> tuple[bool, int, int]:
        if counter not in self.limits:
            raise ValueError(f"Unknown usage counter: {counter}")
        with self._lock:
            self._rollover_locked()
            used = int(self._data.get(counter, 0))
            limit = self.limits[counter]
            if used >= limit:
                return False, used, limit
            used += 1
            self._data[counter] = used
            self._save_locked()
            return True, used, limit

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._rollover_locked()
            return {
                "date": self._data["date"],
                **{
                    name: {
                        "used": int(self._data.get(name, 0)),
                        "limit": self.limits[name],
                    }
                    for name in self.COUNTERS
                },
            }

    @staticmethod
    def _meter(used: int, limit: int) -> str:
        percent = min(100, round(used * 100 / max(1, limit)))
        filled = min(10, round(percent / 10))
        return f"{'█' * filled}{'░' * (10 - filled)} {percent}%"

    def format_status(self, gemini_status: str) -> str:
        data = self.snapshot()
        gemini = data["gemini_requests"]
        vnstock = data["vnstock_requests"]
        deep = data["deep_commands"]
        scan = data["scan_commands"]
        news = data["news_commands"]
        return (
            "<b>Ngân sách API an toàn hôm nay</b>\n"
            f"Gemini: {gemini['used']}/{gemini['limit']} "
            f"— {self._meter(gemini['used'], gemini['limit'])}\n"
            f"VNStock: {vnstock['used']}/{vnstock['limit']} "
            f"— {self._meter(vnstock['used'], vnstock['limit'])}\n"
            f"/deep: {deep['used']}/{deep['limit']} "
            f"— {self._meter(deep['used'], deep['limit'])}\n"
            f"/scan: {scan['used']}/{scan['limit']} "
            f"— {self._meter(scan['used'], scan['limit'])}\n"
            f"/news: {news['used']}/{news['limit']} "
            f"— {self._meter(news['used'], news['limit'])}\n"
            f"Trạng thái Gemini: {html.escape(gemini_status)}\n"
            "Đặt lại lúc 00:00 (UTC+7). Đây là bộ đếm nội bộ, không phải quota trực tiếp từ nhà cung cấp."
        )


class GeminiAnalyzer:
    """Grounded Gemini research for candidates that pass the numeric filter."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL,
        thinking_level: str = DEFAULT_GEMINI_THINKING_LEVEL,
        max_output_tokens: int = DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
        use_google_search: bool = False,
        timeout: float = DEFAULT_GEMINI_TIMEOUT,
        min_interval: float = DEFAULT_GEMINI_MIN_INTERVAL,
        cache_ttl: float = DEFAULT_GEMINI_CACHE_TTL,
        quota_cooldown: float = DEFAULT_GEMINI_QUOTA_COOLDOWN,
        usage_store: ApiUsageStore | None = None,
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
        self.min_interval = max(0.0, float(min_interval))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.quota_cooldown = max(60.0, float(quota_cooldown))
        self.usage_store = usage_store
        self._client_factory = client_factory
        self._search_disabled_until = 0.0
        self._quota_disabled_until = 0.0
        self._last_request_at = 0.0
        self._state_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._cache: dict[str, tuple[float, str]] = {}

    @staticmethod
    def _normalize_model(model: str, default: str) -> str:
        normalized = str(model or "").strip()
        if normalized.startswith("models/"):
            normalized = normalized[len("models/") :]
        return normalized or default

    def enabled(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _is_quota_error(exc: BaseException) -> bool:
        status_values = [
            getattr(exc, "code", None),
            getattr(exc, "status_code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ]
        if any(str(value) == "429" for value in status_values if value is not None):
            return True
        message = f"{type(exc).__name__}: {exc}".lower()
        return any(
            marker in message
            for marker in ("429", "resource_exhausted", "resource exhausted", "quota exceeded")
        )

    def _open_quota_circuit(self) -> None:
        with self._state_lock:
            self._quota_disabled_until = max(
                self._quota_disabled_until,
                time.monotonic() + self.quota_cooldown,
            )

    def _quota_remaining(self) -> float:
        with self._state_lock:
            return max(0.0, self._quota_disabled_until - time.monotonic())

    def _reserve_request_slot(self) -> None:
        with self._request_lock:
            remaining = self._quota_remaining()
            if remaining > 0:
                raise GeminiQuotaCircuitOpen(f"Gemini quota cooldown: {remaining:.0f}s")
            delay = 0.0
            if self._last_request_at > 0:
                delay = self.min_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                raise GeminiRequestCooldown(delay)
            if self.usage_store is not None:
                allowed, _, _ = self.usage_store.claim("gemini_requests")
                if not allowed:
                    raise GeminiDailyBudgetReached("Gemini daily safety budget reached")
            self._last_request_at = time.monotonic()

    def _cached_result(self, symbol: str) -> str | None:
        now = time.monotonic()
        with self._state_lock:
            cached = self._cache.get(symbol)
            if cached is None:
                return None
            expires_at, text = cached
            if expires_at <= now:
                self._cache.pop(symbol, None)
                return None
            return text

    def _store_cached_result(self, symbol: str, text: str) -> None:
        if self.cache_ttl <= 0:
            return
        with self._state_lock:
            self._cache[symbol] = (time.monotonic() + self.cache_ttl, text)

    @staticmethod
    def _quota_message() -> str:
        return (
            "Gemini đang tạm nghỉ do vượt giới hạn API; bot chỉ gửi phần chấm điểm "
            "định lượng. Hệ thống sẽ tự thử lại sau thời gian cooldown."
        )

    @staticmethod
    def _rate_message(remaining: int) -> str:
        return (
            f"Gemini đang giới hạn nhịp để bảo vệ quota; hãy thử lại sau {remaining} giây. "
            "Bot vẫn gửi phần chấm điểm định lượng."
        )

    @staticmethod
    def _daily_budget_message() -> str:
        return (
            "Gemini đã đạt 100% ngân sách an toàn hôm nay; bot chỉ gửi phần chấm điểm "
            "định lượng và sẽ tự mở lại lúc 00:00 (UTC+7)."
        )

    def status_text(self) -> str:
        if not self.enabled():
            return "chưa có API key"
        quota_remaining = self._quota_remaining()
        if quota_remaining > 0:
            return f"tạm nghỉ do quota, thử lại sau khoảng {max(1, int(quota_remaining / 60) + 1)} phút"
        if self.use_google_search and time.monotonic() < self._search_disabled_until:
            search = "Search đang bị quota giới hạn, dùng fallback số liệu"
        elif self.use_google_search:
            search = "Google Search bật"
        else:
            search = "Google Search tắt"
        return f"đã bật ({self.model}, {self.thinking_level}, {search})"

    def _build_prompt(self, signal: DeepSignal, allow_search: bool = True) -> str:
        snapshot = signal.snapshot
        quote = signal.quote
        closes = [bar.close for bar in signal.bars]
        breakdown = signal.breakdown
        plan = signal.trade_plan
        backtest = signal.backtest
        backtest_3m = signal.backtest_3m
        macro = signal.macro or MacroContext()
        web_instruction = (
            "Hãy dùng Google Search để kiểm tra thông tin mới nhất từ nguồn sơ cấp/đáng tin "
            "(công bố doanh nghiệp, HOSE/HNX/SSC, báo cáo tài chính hoặc báo chí tài chính uy tín). "
            "Phân biệt rõ dữ kiện đã kiểm chứng với nhận định. Nếu không tìm được dữ liệu mới, "
            "hãy nói thẳng là chưa đủ dữ liệu."
            if allow_search
            else
            "Chỉ được dùng các số liệu hệ thống và bối cảnh vimo-VN cung cấp; không được tự "
            "đưa tin, số liệu kết quả kinh doanh hay sự kiện không xuất hiện trong đầu vào."
        )
        return (
            "Bạn là chuyên viên hỗ trợ nghiên cứu cổ phiếu Việt Nam. Dữ liệu định lượng "
            "bên dưới do hệ thống cung cấp; không được tự sửa, suy diễn hoặc bịa số còn thiếu. "
            f"{web_instruction} "
            "Điểm 100, target/stop và backtest là kết quả cố định của hệ thống: chỉ được giải "
            "thích, không được thay đổi. Không gọi hit-rate là xác suất tương lai, không khẳng "
            "định lợi nhuận và không ra lệnh mua/bán.\n\n"
            f"Ngày phân tích: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"Mã: {signal.symbol}\n"
            f"Tên: {snapshot.name}\n"
            f"Ngành: {snapshot.sector} / {snapshot.industry}\n"
            f"Giá: {quote.price}\n"
            f"Doanh thu YoY: {snapshot.revenue_growth}%\n"
            f"Lợi nhuận YoY: {snapshot.net_income_growth}%\n"
            f"ROE: {snapshot.return_on_equity}%\n"
            f"D/E: {snapshot.debt_to_equity}x\n"
            f"Current ratio: {snapshot.current_ratio}\n"
            f"P/E: {snapshot.trailing_pe}\n"
            f"P/B: {snapshot.price_to_book}\n"
            f"MA20: {moving_average(closes, 20)}\n"
            f"MA50: {moving_average(closes, 50)}\n"
            f"MA200: {moving_average(closes, 200)}\n"
            f"RSI14: {rsi(closes, 14)}\n"
            f"Điểm tổng: {signal.score}/100\n"
            f"Điểm thành phần: {breakdown}\n"
            f"Kịch bản target/stop: {plan}\n"
            f"Backtest 1 tháng (~20 phiên): {backtest}\n"
            f"Backtest 3 tháng (~60 phiên): {backtest_3m}\n"
            f"Vĩ mô vimo-VN: stance={macro.stance}; score={macro.score}; "
            f"summary={macro.summary}; tích cực={macro.positive_drivers}; "
            f"rủi ro={macro.risk_drivers}; chính sách={macro.policy_notes}; "
            f"confidence={macro.confidence}\n"
            f"Lý do lọc: {', '.join(signal.reasons)}\n\n"
            "Trả lời tiếng Việt ngắn gọn theo đúng 5 mục, mỗi mục 1–2 câu: "
            "1) Chất lượng doanh nghiệp; 2) Định giá; 3) Mẫu hình và điều kiện xác nhận/hủy; "
            "4) Vĩ mô theo hai chiều tích cực-rủi ro; 5) Điều còn thiếu cần kiểm chứng. "
            "Không chèn bảng và không tự viết danh sách nguồn vì hệ thống sẽ gắn nguồn."
        )

    def _create_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self.api_key)
        if genai is None:
            raise RuntimeError("google-genai chưa được cài đặt")
        # The application owns retry/fallback decisions. Keep the SDK from
        # multiplying a single Telegram command into several quota failures.
        retry_options = genai.types.HttpRetryOptions(attempts=1)
        http_options = genai.types.HttpOptions(retry_options=retry_options)
        return genai.Client(api_key=self.api_key, http_options=http_options)

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
        label = (
            "\n\n(Nhận xét AI; không được dùng để thay thế số liệu và "
            "kết quả chấm điểm deterministic.)"
        )
        if not sources:
            return (clean_text + label)[:2600]
        source_lines = ["Nguồn kiểm chứng:"]
        for title, url in sources:
            source_lines.append(f"• {title}: {url}")
        return (
            clean_text + "\n\n" + "\n".join(source_lines) + label
        )[:3200]

    @staticmethod
    def _numeric_tokens(text: str) -> list[float]:
        values: list[float] = []
        for raw in re.findall(r"(?<![\w.])-?\d+(?:[.,]\d+)?", text):
            try:
                values.append(float(raw.replace(",", ".")))
            except ValueError:
                continue
        return values

    @classmethod
    def _validate_numeric_claims(cls, text: str, prompt: str) -> None:
        """Reject any number that did not exist in deterministic input."""
        allowed = cls._numeric_tokens(prompt)
        for claim in cls._numeric_tokens(text):
            if not any(math.isclose(claim, value, rel_tol=1e-9, abs_tol=1e-9) for value in allowed):
                raise RuntimeError(
                    "Gemini added a numeric claim not present in deterministic input"
                )

    def _analyze_interactions(
        self,
        prompt: str,
        use_search: bool | None = None,
        model: str | None = None,
    ) -> str:
        self._reserve_request_slot()
        client = self._create_client()
        if use_search is None:
            use_search = self.use_google_search and time.monotonic() >= self._search_disabled_until
        tools = [{"type": "google_search"}] if use_search else None
        interaction = client.interactions.create(
            model=model or self.model,
            input=prompt,
            tools=tools,
            generation_config={
                "max_output_tokens": self.max_output_tokens,
                "thinking_level": self.thinking_level,
            },
            store=False,
            timeout=self.timeout,
        )
        text, sources = self._extract_interaction(interaction)
        if not text:
            raise RuntimeError("Gemini không trả về nội dung")
        if use_search and not sources:
            raise RuntimeError("Gemini Search returned no grounding sources")
        self._validate_numeric_claims(text, prompt)
        return self._with_sources(text, sources)

    def _analyze_legacy_fallback(self, prompt: str) -> str:
        self._reserve_request_slot()
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{url_quote(self.fallback_model, safe='')}:generateContent"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": min(self.max_output_tokens, 1200),
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
        self._validate_numeric_claims(text, prompt)
        return self._with_sources(text[:2200], [])

    def analyze(self, signal: DeepSignal) -> str:
        if not self.enabled():
            return "Gemini chưa có API key; bot đang dùng phần chấm điểm định lượng."

        cached = self._cached_result(signal.symbol)
        if cached is not None:
            return cached + "\n\n(Kết quả Gemini được lấy từ cache để tiết kiệm quota.)"
        if self._quota_remaining() > 0:
            return self._quota_message()

        search_attempted = self.use_google_search and time.monotonic() >= self._search_disabled_until
        prompt = self._build_prompt(signal, allow_search=search_attempted)
        try:
            result = self._analyze_interactions(prompt, use_search=search_attempted)
            self._store_cached_result(signal.symbol, result)
            return result
        except GeminiRequestCooldown as cooldown_exc:
            return self._rate_message(cooldown_exc.remaining)
        except GeminiDailyBudgetReached:
            return self._daily_budget_message()
        except GeminiQuotaCircuitOpen:
            return self._quota_message()
        except Exception as primary_exc:  # SDK/API failures must not stop Telegram polling.
            LOG.warning(
                "Gemini Interactions failed for %s with model %s: %s",
                signal.symbol,
                self.model,
                type(primary_exc).__name__,
            )
            if self._is_quota_error(primary_exc):
                self._open_quota_circuit()
                LOG.warning(
                    "Gemini quota circuit opened for %.0f seconds.",
                    self.quota_cooldown,
                )
                return self._quota_message()

        if search_attempted:
            self._search_disabled_until = time.monotonic() + 3600.0

        # One fallback only. A quota error never enters this route, preventing
        # the old retry cascade (Search -> no Search -> model -> REST).
        offline_prompt = self._build_prompt(signal, allow_search=False)
        try:
            if genai is None and self._client_factory is None:
                fallback_text = self._analyze_legacy_fallback(offline_prompt)
            else:
                fallback_text = self._analyze_interactions(
                    offline_prompt,
                    use_search=False,
                    model=self.fallback_model,
                )
            suffix = (
                "\n\n(Google Search không khả dụng; Gemini đang dùng model dự phòng "
                "với dữ liệu định lượng.)"
            )
            result = fallback_text + suffix
            self._store_cached_result(signal.symbol, result)
            return result
        except GeminiRequestCooldown as cooldown_exc:
            return self._rate_message(cooldown_exc.remaining)
        except GeminiDailyBudgetReached:
            return self._daily_budget_message()
        except GeminiQuotaCircuitOpen:
            return self._quota_message()
        except Exception as fallback_exc:
            LOG.warning(
                "Gemini fallback failed for %s with model %s: %s",
                signal.symbol,
                self.fallback_model,
                type(fallback_exc).__name__,
            )
            if self._is_quota_error(fallback_exc):
                self._open_quota_circuit()
                return self._quota_message()
            return "Gemini không phản hồi lúc này; bot chỉ gửi phần chấm điểm định lượng."

    def analyze_news(
        self,
        topic: str,
        items: list[NewsItem],
        macro: MacroContext,
    ) -> str:
        """Analyze supplied headlines without inventing article details."""

        if not self.enabled():
            return (
                "Gemini chưa có API key; bot chỉ hiển thị tiêu đề có nguồn và "
                "bối cảnh vĩ mô định lượng."
            )
        cache_key = f"NEWS:{topic.casefold()}"
        cached = self._cached_result(cache_key)
        if cached is not None:
            return cached + "\n\n(Kết quả được lấy từ cache để tiết kiệm quota.)"
        if self._quota_remaining() > 0:
            return self._quota_message()
        headline_lines = "\n".join(
            f"- [{item.source}] {item.title} ({item.published_at or 'không rõ ngày'})"
            for item in items
        ) or "- Không có tiêu đề phù hợp từ RSS."
        prompt = (
            "Bạn phân tích tin doanh nghiệp và vĩ mô Việt Nam theo góc nhìn trung lập. "
            "Chỉ được dùng metadata tiêu đề/nguồn bên dưới; không được giả định đã đọc toàn "
            "bài, không tự bịa nội dung thông tư, số điều, mức tác động hay số liệu. Một tiêu "
            "đề không phải bằng chứng cho quan hệ nhân quả. Nếu các nguồn mâu thuẫn hoặc chưa "
            "đủ dữ kiện, phải ghi rõ. Không ra lệnh mua/bán.\n\n"
            f"Chủ đề: {topic}\n"
            f"Tiêu đề công khai:\n{headline_lines}\n\n"
            f"Bối cảnh vimo-VN: stance={macro.stance}; score={macro.score}; "
            f"summary={macro.summary}; tích cực={macro.positive_drivers}; "
            f"rủi ro={macro.risk_drivers}; trung tính={macro.neutral_drivers}; "
            f"ghi chú chính sách/thông tư={macro.policy_notes}; "
            f"confidence={macro.confidence}\n\n"
            "Trả lời tiếng Việt, tối đa 700 từ, theo 4 mục: "
            "1) Dữ kiện có thể xác nhận từ tiêu đề; 2) Kênh tác động tích cực; "
            "3) Kênh tác động tiêu cực/rủi ro; 4) Điều cần mở văn bản gốc hoặc công bố "
            "doanh nghiệp để kiểm chứng. Không thêm danh sách nguồn vì hệ thống tự gắn."
        )
        try:
            result = self._analyze_interactions(prompt, use_search=False)
            self._store_cached_result(cache_key, result)
            return result
        except GeminiRequestCooldown as cooldown_exc:
            return self._rate_message(cooldown_exc.remaining)
        except GeminiDailyBudgetReached:
            return self._daily_budget_message()
        except GeminiQuotaCircuitOpen:
            return self._quota_message()
        except Exception as exc:
            LOG.warning("Gemini news analysis failed: %s", type(exc).__name__)
            if self._is_quota_error(exc):
                self._open_quota_circuit()
                return self._quota_message()
            return (
                "Gemini không phản hồi lúc này; bot vẫn giữ danh sách tiêu đề có nguồn "
                "và bối cảnh vimo-VN để bạn tự kiểm chứng."
            )


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


def _clamp_points(value: float, maximum: int) -> int:
    return max(0, min(maximum, int(round(value))))


def average_true_range(bars: list[PriceBar], window: int = 14) -> float | None:
    if len(bars) < 2:
        return None
    true_ranges: list[float] = []
    previous_close = bars[0].close
    for bar in bars[1:]:
        high = bar.high if bar.high is not None else bar.close
        low = bar.low if bar.low is not None else bar.close
        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )
        previous_close = bar.close
    if not true_ranges:
        return None
    sample = true_ranges[-window:]
    return sum(sample) / len(sample)


def _technical_profile(
    bars: list[PriceBar],
    price: float,
) -> tuple[str, dict[str, float | None]]:
    closes = [bar.close for bar in bars]
    ma20 = moving_average(closes, 20)
    ma50 = moving_average(closes, 50)
    ma200 = moving_average(closes, 200)
    rsi14 = rsi(closes, 14)
    recent_before = closes[-21:-1] if len(closes) >= 21 else closes[:-1]
    prior_high = max(recent_before) if recent_before else None
    volumes = [float(bar.volume) for bar in bars[-21:-1] if bar.volume is not None]
    average_volume = sum(volumes) / len(volumes) if volumes else None
    latest_volume = float(bars[-1].volume) if bars and bars[-1].volume is not None else None
    volume_ratio = (
        latest_volume / average_volume
        if latest_volume is not None and average_volume and average_volume > 0
        else None
    )

    breakout = (
        prior_high is not None
        and price >= prior_high
        and volume_ratio is not None
        and volume_ratio >= 1.2
    )
    uptrend = (
        ma20 is not None
        and ma50 is not None
        and price >= ma20
        and ma20 >= ma50
        and (ma200 is None or ma50 >= ma200)
    )
    pullback = (
        ma20 is not None
        and ma50 is not None
        and ma20 >= ma50
        and ma20 * 0.97 <= price <= ma20 * 1.03
    )
    if breakout:
        pattern = "bứt phá có xác nhận khối lượng"
    elif pullback:
        pattern = "điều chỉnh về MA20 trong xu hướng tăng"
    elif uptrend:
        pattern = "xu hướng tăng"
    elif ma20 is not None and ma50 is not None and abs(ma20 / ma50 - 1.0) <= 0.03:
        pattern = "tích lũy/đi ngang"
    elif ma20 is not None and price < ma20:
        pattern = "xu hướng yếu, chưa xác nhận đảo chiều"
    else:
        pattern = "chưa đủ dữ liệu nhận dạng"
    return pattern, {
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "rsi14": rsi14,
        "volume_ratio": volume_ratio,
    }


def build_score_breakdown(
    snapshot: FundamentalSnapshot,
    quote: Quote,
    bars: list[PriceBar],
    macro: MacroContext | None = None,
) -> tuple[ScoreBreakdown, list[str]]:
    """Score five auditable dimensions; missing data receives neutral/low points."""

    reasons: list[str] = []
    price = snapshot.price or quote.price

    # 1) Business quality: 30 points.
    revenue_growth = snapshot.revenue_growth
    if revenue_growth is None:
        revenue_points = 2
    elif revenue_growth >= 15:
        revenue_points = 5
        reasons.append(f"doanh thu tăng {revenue_growth:.1f}%")
    elif revenue_growth >= 5:
        revenue_points = 4
    elif revenue_growth >= 0:
        revenue_points = 3
    elif revenue_growth >= -10:
        revenue_points = 1
        reasons.append(f"doanh thu giảm {abs(revenue_growth):.1f}%")
    else:
        revenue_points = 0
        reasons.append(f"doanh thu giảm mạnh {abs(revenue_growth):.1f}%")

    profit_growth = snapshot.net_income_growth
    if profit_growth is None:
        profit_points = 2
    elif profit_growth >= 20:
        profit_points = 5
        reasons.append(f"lợi nhuận tăng {profit_growth:.1f}%")
    elif profit_growth >= 8:
        profit_points = 4
    elif profit_growth >= 0:
        profit_points = 3
    elif profit_growth >= -15:
        profit_points = 1
        reasons.append(f"lợi nhuận giảm {abs(profit_growth):.1f}%")
    else:
        profit_points = 0
        reasons.append(f"lợi nhuận giảm mạnh {abs(profit_growth):.1f}%")

    roe = snapshot.return_on_equity
    if roe is None:
        roe_points = 5
    elif roe >= 20:
        roe_points = 10
        reasons.append(f"ROE cao {roe:.1f}%")
    elif roe >= 12:
        roe_points = 8
    elif roe >= 8:
        roe_points = 6
    elif roe > 0:
        roe_points = 3
    else:
        roe_points = 0
        reasons.append("ROE không dương")

    sector_text = f"{snapshot.sector or ''} {snapshot.industry or ''}".casefold()
    is_financial = any(
        token in sector_text
        for token in ("finance", "bank", "insurance", "financial", "ngân hàng")
    )
    if is_financial:
        balance_points = 5
        reasons.append("đòn bẩy ngành tài chính cần so với nhóm đồng ngành")
    else:
        debt = snapshot.debt_to_equity
        current = snapshot.current_ratio
        if debt is None:
            debt_points = 2
        elif debt < 0.5:
            debt_points = 5
        elif debt < 1.0:
            debt_points = 4
        elif debt < 1.5:
            debt_points = 2
        else:
            debt_points = 0
            reasons.append(f"D/E cao {debt:.2f}x")
        if current is None:
            current_points = 2
        elif current >= 1.5:
            current_points = 5
        elif current >= 1.0:
            current_points = 4
        elif current >= 0.7:
            current_points = 2
        else:
            current_points = 0
            reasons.append(f"thanh toán hiện hành thấp {current:.2f}")
        balance_points = debt_points + current_points
    business = _clamp_points(
        revenue_points + profit_points + roe_points + balance_points,
        30,
    )

    # 2) Valuation: 25 points.
    pe = snapshot.trailing_pe
    if pe is None:
        pe_points = 5
    elif 0 < pe <= 8:
        pe_points = 12
        reasons.append(f"P/E thấp {pe:.2f}")
    elif pe <= 12:
        pe_points = 10
    elif pe <= 18:
        pe_points = 8
    elif pe <= 25:
        pe_points = 5
    elif pe <= 40:
        pe_points = 2
    else:
        pe_points = 0
        if pe <= 0:
            reasons.append("P/E không có ý nghĩa do lợi nhuận không dương")

    pb = snapshot.price_to_book
    if pb is None:
        pb_points = 3
    elif 0 < pb <= 1:
        pb_points = 8
        reasons.append(f"P/B thấp {pb:.2f}")
    elif pb <= 1.5:
        pb_points = 7
    elif pb <= 2.5:
        pb_points = 5
    elif pb <= 4:
        pb_points = 2
    else:
        pb_points = 0

    growth_for_peg = snapshot.earnings_growth
    if growth_for_peg is None:
        growth_for_peg = snapshot.net_income_growth
    if pe is not None and pe > 0 and growth_for_peg is not None and growth_for_peg > 0:
        peg = pe / growth_for_peg
        if peg <= 0.75:
            growth_points = 5
        elif peg <= 1.25:
            growth_points = 4
        elif peg <= 2:
            growth_points = 2
        else:
            growth_points = 0
    else:
        growth_points = 2 if growth_for_peg is None else 0
    valuation = _clamp_points(pe_points + pb_points + growth_points, 25)

    # 3) Trend, momentum, pattern, and volume: 25 points.
    pattern, profile = _technical_profile(bars, price)
    ma20 = profile["ma20"]
    ma50 = profile["ma50"]
    ma200 = profile["ma200"]
    rsi14 = profile["rsi14"]
    volume_ratio = profile["volume_ratio"]
    trend_points = 0
    if ma20 is not None and price >= ma20:
        trend_points += 3
    if ma20 is not None and ma50 is not None and ma20 >= ma50:
        trend_points += 4
    if ma50 is not None and ma200 is not None and ma50 >= ma200:
        trend_points += 5
    if rsi14 is None:
        momentum_points = 2
    elif 45 <= rsi14 <= 65:
        momentum_points = 5
    elif 35 <= rsi14 <= 70:
        momentum_points = 4
    elif 30 <= rsi14 <= 75:
        momentum_points = 2
    else:
        momentum_points = 0
        reasons.append(f"RSI14 ở vùng cực trị {rsi14:.1f}")
    pattern_points = {
        "bứt phá có xác nhận khối lượng": 5,
        "điều chỉnh về MA20 trong xu hướng tăng": 4,
        "xu hướng tăng": 4,
        "tích lũy/đi ngang": 3,
        "xu hướng yếu, chưa xác nhận đảo chiều": 0,
        "chưa đủ dữ liệu nhận dạng": 1,
    }.get(pattern, 1)
    if volume_ratio is None:
        volume_points = 1
    elif volume_ratio >= 1.2:
        volume_points = 3
    elif volume_ratio >= 0.8:
        volume_points = 2
    else:
        volume_points = 1
    technical = _clamp_points(
        trend_points + momentum_points + pattern_points + volume_points,
        25,
    )
    reasons.append(f"mẫu hình: {pattern}")

    # 4) Volatility and liquidity risk: 10 points.
    closes = [bar.close for bar in bars]
    returns = [
        current / previous - 1.0
        for previous, current in zip(closes[:-1], closes[1:])
        if previous > 0
    ]
    if len(returns) >= 20:
        mean_return = sum(returns) / len(returns)
        variance = sum((item - mean_return) ** 2 for item in returns) / len(returns)
        annual_volatility = math.sqrt(variance) * math.sqrt(252) * 100
        if annual_volatility <= 20:
            volatility_points = 5
        elif annual_volatility <= 30:
            volatility_points = 4
        elif annual_volatility <= 45:
            volatility_points = 3
        elif annual_volatility <= 60:
            volatility_points = 1
        else:
            volatility_points = 0
            reasons.append(f"biến động năm hóa cao {annual_volatility:.1f}%")
    else:
        volatility_points = 2
    trading_values = [
        bar.close * float(bar.volume)
        for bar in bars[-20:]
        if bar.volume is not None and bar.volume > 0
    ]
    average_trading_value = (
        sum(trading_values) / len(trading_values) if trading_values else None
    )
    if average_trading_value is None:
        liquidity_points = 2
    elif average_trading_value >= 20_000_000_000:
        liquidity_points = 5
    elif average_trading_value >= 5_000_000_000:
        liquidity_points = 4
    elif average_trading_value >= 1_000_000_000:
        liquidity_points = 3
    elif average_trading_value >= 200_000_000:
        liquidity_points = 1
    else:
        liquidity_points = 0
        reasons.append("giá trị giao dịch bình quân thấp")
    risk_points = _clamp_points(volatility_points + liquidity_points, 10)

    # 5) vimo-VN macro overlay: 10 points, neutral when unavailable.
    if macro is not None and macro.available and macro.score is not None:
        macro_points = _clamp_points(5 + macro.score / 2.0, 10)
    else:
        macro_points = 5
    total = business + valuation + technical + risk_points + macro_points
    breakdown = ScoreBreakdown(
        business=business,
        valuation=valuation,
        technical=technical,
        risk=risk_points,
        macro=macro_points,
        total=_clamp_points(total, 100),
        pattern=pattern,
    )
    if not reasons:
        reasons.append("dữ liệu hiện tại chưa tạo ra điểm nổi bật")
    return breakdown, reasons


def score_candidate(
    snapshot: FundamentalSnapshot,
    quote: Quote,
    bars: list[PriceBar],
    macro: MacroContext | None = None,
) -> tuple[int, list[str]]:
    breakdown, reasons = build_score_breakdown(snapshot, quote, bars, macro)
    return breakdown.total, reasons


def build_trade_plan(quote: Quote, bars: list[PriceBar]) -> TradePlan:
    price = quote.price
    atr14 = average_true_range(bars, 14)
    atr_risk = 1.5 * atr14 if atr14 is not None else price * 0.05
    recent = bars[-20:]
    lows = [
        bar.low if bar.low is not None else bar.close
        for bar in recent
    ]
    highs = [
        bar.high if bar.high is not None else bar.close
        for bar in recent
    ]
    support = min(lows) if lows else None
    resistance = max(highs) if highs else None
    risk_distance = max(price * 0.03, atr_risk)
    if support is not None and 0 < support < price:
        risk_distance = max(risk_distance, min(price - support, price * 0.10))
    risk_distance = min(risk_distance, price * 0.10)
    stop = price - risk_distance
    return TradePlan(
        entry_low=price * 0.99,
        entry_high=price * 1.01,
        stop=stop,
        target_1=price + risk_distance,
        target_2=price + 2 * risk_distance,
        target_3=price + 3 * risk_distance,
        risk_pct=risk_distance / price * 100 if price else 0.0,
        support=support,
        resistance=resistance,
    )


def _market_signature(closes: list[float], index: int) -> tuple[int, int] | None:
    if index < 50:
        return None
    segment = closes[: index + 1]
    price = closes[index]
    ma20 = sum(closes[index - 19 : index + 1]) / 20
    ma50 = sum(closes[index - 49 : index + 1]) / 50
    ma200 = (
        sum(closes[index - 199 : index + 1]) / 200
        if index >= 199
        else None
    )
    if price >= ma20 and ma20 >= ma50 and (ma200 is None or ma50 >= ma200):
        trend_state = 1
    elif price < ma20 and ma20 < ma50:
        trend_state = -1
    else:
        trend_state = 0
    current_rsi = rsi(segment, 14)
    if current_rsi is None:
        rsi_bucket = 1
    elif current_rsi < 35:
        rsi_bucket = 0
    elif current_rsi <= 65:
        rsi_bucket = 1
    else:
        rsi_bucket = 2
    return trend_state, rsi_bucket


def backtest_similar_patterns(
    bars: list[PriceBar],
    plan: TradePlan,
    lookahead_sessions: int = 20,
) -> BacktestResult:
    """Test T1-before-stop on similar trend/RSI states, conservatively."""

    closes = [bar.close for bar in bars]
    if len(closes) < 80 or plan.risk_pct <= 0:
        return BacktestResult(
            samples=0,
            wins=0,
            losses=0,
            unresolved=0,
            hit_rate=None,
            lookahead_sessions=lookahead_sessions,
        )
    current_signature = _market_signature(closes, len(closes) - 1)
    if current_signature is None:
        return BacktestResult(
            samples=0,
            wins=0,
            losses=0,
            unresolved=0,
            hit_rate=None,
            lookahead_sessions=lookahead_sessions,
        )
    candidate_indices: list[int] = []
    last_possible = len(closes) - lookahead_sessions - 1
    for index in range(50, max(50, last_possible + 1)):
        if _market_signature(closes, index) == current_signature:
            candidate_indices.append(index)
    candidate_indices = candidate_indices[-40:]
    risk_ratio = plan.risk_pct / 100.0
    wins = 0
    losses = 0
    unresolved = 0
    holding_returns: list[float] = []
    for index in candidate_indices:
        entry = closes[index]
        exit_price = closes[index + lookahead_sessions]
        holding_returns.append((exit_price / entry - 1.0) * 100.0)
        target = entry * (1.0 + risk_ratio)
        stop = entry * (1.0 - risk_ratio)
        outcome = ""
        for bar in bars[index + 1 : index + 1 + lookahead_sessions]:
            high = bar.high if bar.high is not None else bar.close
            low = bar.low if bar.low is not None else bar.close
            hit_target = high >= target
            hit_stop = low <= stop
            if hit_stop:
                # If both are touched in one daily candle, count the stop first.
                outcome = "loss"
                break
            if hit_target:
                outcome = "win"
                break
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        else:
            unresolved += 1
    resolved = wins + losses
    hit_rate = wins / resolved * 100 if resolved >= 5 else None
    positive_closes = sum(1 for item in holding_returns if item > 0)
    positive_close_rate = (
        positive_closes / len(holding_returns) * 100
        if len(holding_returns) >= 5
        else None
    )
    median_return = None
    if holding_returns:
        ordered_returns = sorted(holding_returns)
        middle = len(ordered_returns) // 2
        if len(ordered_returns) % 2:
            median_return = ordered_returns[middle]
        else:
            median_return = (
                ordered_returns[middle - 1] + ordered_returns[middle]
            ) / 2.0
    return BacktestResult(
        samples=len(candidate_indices),
        wins=wins,
        losses=losses,
        unresolved=unresolved,
        hit_rate=hit_rate,
        positive_closes=positive_closes,
        positive_close_rate=positive_close_rate,
        median_return=median_return,
        lookahead_sessions=lookahead_sessions,
    )


def build_deep_signal(
    symbol: str,
    snapshot: FundamentalSnapshot,
    quote: Quote,
    bars: list[PriceBar],
    macro: MacroContext | None = None,
) -> DeepSignal:
    breakdown, reasons = build_score_breakdown(snapshot, quote, bars, macro)
    plan = build_trade_plan(quote, bars)
    backtest = backtest_similar_patterns(bars, plan, 20)
    backtest_3m = backtest_similar_patterns(bars, plan, 60)
    return DeepSignal(
        symbol=symbol,
        score=breakdown.total,
        snapshot=snapshot,
        quote=quote,
        bars=bars,
        reasons=reasons,
        breakdown=breakdown,
        macro=macro,
        trade_plan=plan,
        backtest=backtest,
        backtest_3m=backtest_3m,
    )


def _plain_excerpt(text: str, maximum: int) -> str:
    clean = re.sub(r"\s+\n", "\n", str(text or "").strip())
    if len(clean) <= maximum:
        return clean
    return clean[: max(0, maximum - 1)].rstrip() + "…"


def _compose_telegram_html(
    prefix: str,
    plain_text: str,
    suffix: str,
    limit: int = 4090,
) -> str:
    """Fit escaped model text without cutting an HTML entity or closing tag."""

    clean = str(plain_text or "").strip()
    while clean:
        rendered = prefix + html.escape(clean) + suffix
        if len(rendered) <= limit:
            return rendered
        overflow = len(rendered) - limit
        clean = clean[: max(0, len(clean) - max(overflow, 24))].rstrip()
        if clean:
            clean = clean.rstrip("…") + "…"
    rendered = prefix + suffix
    return rendered if len(rendered) <= limit else prefix[: max(0, limit - len(suffix))] + suffix


def _format_backtest_horizon(label: str, result: BacktestResult) -> str:
    if result.samples == 0:
        return f"{label}: chưa có đủ lịch sử phù hợp."
    resolved = result.wins + result.losses
    if result.hit_rate is None:
        target_text = (
            f"T1 trước stop: chưa đủ mẫu kết luận "
            f"({result.wins} đạt/{result.losses} dừng/{result.unresolved} chưa ngã ngũ)"
        )
    else:
        target_text = (
            f"T1 trước stop {result.hit_rate:.1f}% "
            f"({result.wins}/{resolved} mẫu đã ngã ngũ; "
            f"{result.unresolved} chưa ngã ngũ)"
        )
    if result.positive_close_rate is None:
        holding_text = "giá cuối kỳ dương: chưa đủ mẫu"
    else:
        median_text = (
            f"{result.median_return:+.1f}%"
            if result.median_return is not None
            else "—"
        )
        holding_text = (
            f"giá cuối kỳ dương {result.positive_close_rate:.1f}% "
            f"({result.positive_closes}/{result.samples}); "
            f"lợi nhuận trung vị {median_text}"
        )
    return f"{label}: {target_text}; {holding_text}."


def format_deep_signal(signal: DeepSignal, gemini_text: str) -> str:
    snapshot = signal.snapshot
    price = snapshot.price or signal.quote.price
    breakdown = signal.breakdown
    if breakdown is None:
        breakdown, _ = build_score_breakdown(
            snapshot,
            signal.quote,
            signal.bars,
            signal.macro,
        )
    plan = signal.trade_plan or build_trade_plan(signal.quote, signal.bars)
    backtest = signal.backtest or backtest_similar_patterns(signal.bars, plan)
    backtest_3m = signal.backtest_3m or backtest_similar_patterns(
        signal.bars,
        plan,
        60,
    )
    macro = signal.macro or MacroContext()
    macro_score = (
        format_number(macro.score, 1)
        if macro.available and macro.score is not None
        else "—"
    )
    backtest_1m_line = _format_backtest_horizon("1 tháng (~20 phiên)", backtest)
    backtest_3m_line = _format_backtest_horizon("3 tháng (~60 phiên)", backtest_3m)
    source_line = ""
    if macro.sources:
        source = macro.sources[0]
        source_line = (
            "\nNguồn vĩ mô: "
            f'<a href="{html.escape(source.url, quote=True)}">'
            f"{html.escape(source.title)}</a>"
        )
    drivers = list(macro.positive_drivers[:1]) + list(macro.risk_drivers[:2])
    driver_line = "; ".join(drivers) if drivers else macro.summary
    reasons = "; ".join(signal.reasons[:7])
    prefix = (
        f"<b>DEEP 100 điểm: {html.escape(signal.symbol)}</b>\n"
        f"{html.escape(snapshot.name)}"
        f"{' — ' + html.escape(snapshot.sector) if snapshot.sector else ''}\n"
        f"Tổng: <b>{breakdown.total}/100</b> | "
        f"DN {breakdown.business}/30 · Định giá {breakdown.valuation}/25 · "
        f"Kỹ thuật {breakdown.technical}/25 · Rủi ro {breakdown.risk}/10 · "
        f"Vĩ mô {breakdown.macro}/10\n\n"
        "<b>1) Doanh nghiệp &amp; định giá</b>\n"
        f"Giá: <b>{format_number(price)}</b> VND | "
        f"P/E {format_number(snapshot.trailing_pe)} | P/B {format_number(snapshot.price_to_book)}\n"
        f"Doanh thu YoY {format_number(snapshot.revenue_growth)}% | "
        f"LN YoY {format_number(snapshot.net_income_growth)}% | "
        f"ROE {format_number(snapshot.return_on_equity)}%\n"
        f"D/E {format_number(snapshot.debt_to_equity)}x | "
        f"Current ratio {format_number(snapshot.current_ratio)}\n\n"
        "<b>2) Mẫu hình &amp; kịch bản giá</b>\n"
        f"Mẫu hình: {html.escape(breakdown.pattern)}\n"
        f"Vùng theo dõi: {format_number(plan.entry_low)}–{format_number(plan.entry_high)}\n"
        f"Mốc vô hiệu/stop giả định: {format_number(plan.stop)} "
        f"(rủi ro {plan.risk_pct:.1f}%)\n"
        f"T1/T2/T3: {format_number(plan.target_1)} / "
        f"{format_number(plan.target_2)} / {format_number(plan.target_3)}\n\n"
        "<b>3) Thống kê mẫu hình lịch sử</b>\n"
        f"{html.escape(backtest_1m_line)}\n"
        f"{html.escape(backtest_3m_line)}\n"
        "T1/stop dùng cùng tỷ lệ rủi ro hiện tại; cùng nến chạm cả hai được tính là stop.\n\n"
        "<b>4) Bối cảnh vĩ mô trung lập</b>\n"
        f"{html.escape(macro.stance)} (điểm vimo-VN {macro_score}) — "
        f"{html.escape(_plain_excerpt(driver_line, 420))}"
        f"{source_line}\n\n"
        f"<b>Điểm cần chú ý:</b> {html.escape(_plain_excerpt(reasons, 520))}\n\n"
        "<b>Góc nhìn Gemini:</b>\n"
    )
    suffix = (
        "\n\n<i>Điểm số và tỷ lệ trên là thống kê máy từ dữ liệu quá khứ, "
        "không phải xác suất tương lai hay khuyến nghị mua/bán. Target/stop chỉ là "
        "kịch bản để đo rủi ro; cần kiểm tra báo cáo tài chính, công bố và thanh khoản.</i>"
    )
    gemini_limit = max(240, 3850 - len(prefix) - len(suffix))
    return _compose_telegram_html(
        prefix,
        _plain_excerpt(gemini_text, gemini_limit),
        suffix,
    )


def format_news_report(
    topic: str,
    items: list[NewsItem],
    macro: MacroContext,
    analysis: str,
) -> str:
    """Render a source-bound, neutral news/macro brief."""

    headline_lines: list[str] = []
    for index, item in enumerate(items[:DEFAULT_NEWS_MAX_ITEMS], start=1):
        headline_lines.append(
            f'{index}. <a href="{html.escape(item.url, quote=True)}">'
            f"{html.escape(_plain_excerpt(item.title, 170))}</a> "
            f"— {html.escape(item.source)}"
        )
    if not headline_lines:
        headline_lines.append("Chưa lấy được tiêu đề phù hợp; không suy diễn nội dung.")
    macro_source = ""
    if macro.sources:
        source = macro.sources[0]
        macro_source = (
            f'\n<a href="{html.escape(source.url, quote=True)}">'
            f"Nguồn vĩ mô: {html.escape(source.title)}</a>"
        )
    policy_line = ""
    if macro.policy_notes:
        policy_line = (
            "\nChính sách/thông tư trong vimo-VN: "
            + html.escape(_plain_excerpt(macro.policy_notes[0], 420))
        )
    prefix = (
        f"<b>Phân tích tin trung lập: {html.escape(_plain_excerpt(topic, 120))}</b>\n\n"
        "<b>Các tiêu đề dùng làm đầu vào</b>\n"
        + "\n".join(headline_lines)
        + "\n\n<b>Bối cảnh vimo-VN</b>\n"
        + f"{html.escape(macro.stance)}"
        + (f" · điểm {format_number(macro.score, 1)}" if macro.score is not None else "")
        + f" — {html.escape(_plain_excerpt(macro.summary, 420))}"
        + policy_line
        + macro_source
        + "\n\n<b>Đánh giá hai chiều</b>\n"
    )
    suffix = (
        "\n\n<i>Bot chỉ đọc metadata tiêu đề và bối cảnh vimo-VN; hãy mở bài/văn bản gốc "
        "trước khi kết luận. Đây không phải khuyến nghị đầu tư.</i>"
    )
    analysis_limit = max(300, 3850 - len(prefix) - len(suffix))
    return _compose_telegram_html(
        prefix,
        _plain_excerpt(analysis, analysis_limit),
        suffix,
    )


class DeepSignalScanner:
    """Scan VN100 and emit at most a few high-conviction discount signals."""

    def __init__(
        self,
        provider: YahooQuoteProvider,
        gemini: GeminiAnalyzer,
        symbols: list[str],
        macro_client: MacroContextClient | None = None,
        min_score: int = 70,
        max_per_scan: int = 2,
        max_workers: int = DEFAULT_SCAN_WORKERS,
    ):
        self.provider = provider
        self.gemini = gemini
        self.symbols = symbols
        self.macro_client = macro_client
        self.min_score = min_score
        self.max_per_scan = max_per_scan
        self.max_workers = max(1, min(int(max_workers), 12))

    @staticmethod
    def _fundamental_ceiling(snapshot: FundamentalSnapshot) -> int:
        dummy_price = snapshot.price or 1.0
        dummy_quote = Quote(snapshot.symbol, snapshot.name, dummy_price, dummy_price)
        baseline, _ = build_score_breakdown(snapshot, dummy_quote, [], None)
        # Business and valuation are known after the batch request. Assume the
        # maximum for price/risk/macro so a potentially eligible symbol is
        # never discarded before its history is loaded.
        return min(100, baseline.business + baseline.valuation + 25 + 10 + 10)

    def _evaluate(
        self,
        symbol: str,
        snapshot: FundamentalSnapshot,
        macro: MacroContext | None,
    ) -> DeepSignal | None:
        quote = self.provider.get_quote(symbol)
        bars = self.provider.get_history(symbol, range_value="1y", interval="1d")
        signal = build_deep_signal(symbol, snapshot, quote, bars, macro)
        if signal.score < self.min_score:
            return None
        return signal

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

        macro = self.macro_client.latest() if self.macro_client is not None else None
        # Avoid quote/history requests for symbols that cannot mathematically
        # reach the threshold even with perfect technical, risk, and macro scores.
        eligible = [
            symbol
            for symbol in self.symbols
            if symbol in snapshots
            and self._fundamental_ceiling(snapshots[symbol]) >= self.min_score
        ]
        candidates: list[DeepSignal] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(eligible) or 1)) as executor:
            future_symbols = {
                executor.submit(self._evaluate, symbol, snapshots[symbol], macro): symbol
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


def split_telegram_html(text: str, limit: int = 4090) -> list[str]:
    """Split only at paragraph/line boundaries so closed HTML tags stay valid."""

    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
            continue
        # Normal bot sections are far below the limit. This fallback handles
        # unexpectedly long plain/escaped model lines without looping forever.
        for line in paragraph.splitlines() or [paragraph]:
            if len(line) > limit:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(
                    line[index : index + limit]
                    for index in range(0, len(line), limit)
                )
            else:
                candidate = line if not current else current + "\n" + line
                if len(candidate) <= limit:
                    current = candidate
                else:
                    chunks.append(current)
                    current = line
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


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
        for chunk in split_telegram_html(text):
            self._call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
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
    "/deep <code>FPT</code> — doanh nghiệp, định giá, mẫu hình, điểm 100, target/stop và backtest\n"
    "/news <code>FPT</code> — phân tích tin doanh nghiệp/chủ đề theo hai chiều, có nguồn\n"
    "/new <code>FPT</code> — bí danh ngắn của /news, chỉ giữ tin đúng mã/doanh nghiệp\n"
    "/macro — tin và trạng thái vĩ mô trung lập từ vimo-VN\n"
    "/signals_on — nhận tín hiệu lọc sâu VN100\n"
    "/signals_off — tắt tín hiệu lọc sâu\n"
    "/signals_status — trạng thái nhận tín hiệu\n"
    "/scan — quét VN100 ngay\n"
    "/market — VN-Index\n"
    "/add <code>FPT</code> — thêm vào danh sách theo dõi\n"
    "/remove <code>FPT</code> — xóa khỏi danh sách\n"
    "/watchlist — xem danh sách đã lưu\n"
    "/watch — lấy giá toàn bộ danh sách\n"
    "/usage — xem phần trăm ngân sách API trong ngày\n"
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


def build_gemini_analyzer(usage_store: ApiUsageStore | None = None) -> GeminiAnalyzer:
    try:
        max_tokens = int(
            os.environ.get(
                "GEMINI_MAX_OUTPUT_TOKENS",
                str(DEFAULT_GEMINI_MAX_OUTPUT_TOKENS),
            )
        )
        timeout = float(os.environ.get("GEMINI_TIMEOUT", str(DEFAULT_GEMINI_TIMEOUT)))
        min_interval = float(
            os.environ.get("GEMINI_MIN_INTERVAL", str(DEFAULT_GEMINI_MIN_INTERVAL))
        )
        cache_ttl = float(
            os.environ.get("GEMINI_CACHE_TTL", str(DEFAULT_GEMINI_CACHE_TTL))
        )
        quota_cooldown = float(
            os.environ.get("GEMINI_QUOTA_COOLDOWN", str(DEFAULT_GEMINI_QUOTA_COOLDOWN))
        )
    except ValueError as exc:
        raise ValueError("Các biến GEMINI_* timeout/token/interval/cache/cooldown phải là số.") from exc
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
        use_google_search=parse_bool(os.environ.get("GEMINI_GOOGLE_SEARCH"), False),
        timeout=timeout,
        min_interval=min_interval,
        cache_ttl=cache_ttl,
        quota_cooldown=quota_cooldown,
        usage_store=usage_store,
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
        usage_store: ApiUsageStore | None = None,
        macro_client: MacroContextClient | None = None,
        news_service: NeutralNewsService | None = None,
        monthly_signal_limit: int = DEFAULT_MONTHLY_SIGNAL_LIMIT,
        signal_cooldown_days: int = DEFAULT_SIGNAL_COOLDOWN_DAYS,
        research_command_cooldown: float = DEFAULT_RESEARCH_COMMAND_COOLDOWN,
    ):
        self.telegram = telegram
        self.provider = provider
        self.store = store
        self.signal_store = signal_store
        self.scanner = scanner
        self.usage_store = usage_store
        self.macro_client = macro_client
        self.news_service = news_service
        self.monthly_signal_limit = monthly_signal_limit
        self.signal_cooldown_days = signal_cooldown_days
        self.research_command_cooldown = max(0.0, float(research_command_cooldown))
        self._research_last_started_at = 0.0
        self._research_lock = threading.Lock()

    def _claim_research_slot(self) -> int:
        with self._research_lock:
            now = time.monotonic()
            remaining = 0.0
            if self._research_last_started_at > 0:
                remaining = self.research_command_cooldown - (
                    now - self._research_last_started_at
                )
            if remaining > 0:
                return max(1, int(remaining) + 1)
            self._research_last_started_at = now
            return 0

    @staticmethod
    def _research_cooldown_message(remaining: int) -> str:
        return (
            f"Để tránh vượt giới hạn API, /deep, /scan và /news chỉ chạy một lần mỗi phút. "
            f"Hãy thử lại sau {remaining} giây. /ping và /quote vẫn hoạt động bình thường."
        )

    def _claim_daily_command(self, counter: str, label: str) -> str | None:
        if self.usage_store is None:
            return None
        allowed, used, limit = self.usage_store.claim(counter)
        if allowed:
            return None
        return (
            f"{label} đã dùng {used}/{limit} lượt (100%) trong ngày. "
            "Hãy chờ đến 00:00 (UTC+7) hoặc dùng /usage để xem trạng thái."
        )

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
            if command == "/usage":
                if self.usage_store is None:
                    return "Bộ đếm API chưa được cấu hình."
                gemini_status = (
                    self.scanner.gemini.status_text()
                    if self.scanner is not None
                    else "chưa cấu hình"
                )
                result = self.usage_store.format_status(gemini_status)
                provider_status = getattr(self.provider, "status_text", None)
                if callable(provider_status):
                    status_text = provider_status()
                    if isinstance(status_text, str) and status_text:
                        result += "\n\n" + status_text
                return result
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
                remaining = self._claim_research_slot()
                if remaining:
                    return self._research_cooldown_message(remaining)
                daily_limit = self._claim_daily_command("deep_commands", "/deep")
                if daily_limit:
                    return daily_limit
                quote = self.provider.get_quote(symbol)
                bars = self.provider.get_history(symbol, range_value="2y", interval="1d")
                snapshot = self.provider.get_fundamentals(symbol)
                macro = (
                    self.macro_client.latest()
                    if self.macro_client is not None
                    else None
                )
                scanner = self.scanner or DeepSignalScanner(
                    self.provider,
                    build_gemini_analyzer(self.usage_store),
                    [symbol],
                    macro_client=self.macro_client,
                )
                return scanner.render_signal(
                    build_deep_signal(symbol, snapshot, quote, bars, macro)
                )
            if command in {"/news", "/new", "/tintuc", "/macro"}:
                argument = _argument(text)
                if command != "/macro" and not argument:
                    return (
                        "Dùng: /news <mã hoặc chủ đề>, ví dụ /news FPT "
                        "hoặc /news Thông tư 14/2026."
                    )
                topic = argument or "kinh tế vĩ mô Việt Nam"
                remaining = self._claim_research_slot()
                if remaining:
                    return self._research_cooldown_message(remaining)
                daily_limit = self._claim_daily_command("news_commands", "/news")
                if daily_limit:
                    return daily_limit
                if self.news_service is None:
                    return "Nguồn tin RSS chưa được cấu hình."
                macro = (
                    self.macro_client.latest()
                    if self.macro_client is not None
                    else MacroContext()
                )
                analysis_topic = topic
                try:
                    stock_symbol = (
                        normalize_symbol(topic)
                        if SYMBOL_RE.fullmatch(topic.strip().upper())
                        else None
                    )
                    if stock_symbol:
                        company_name = stock_symbol
                        try:
                            company_name = self.provider.get_fundamentals(
                                stock_symbol
                            ).name
                        except BotError:
                            try:
                                company_name = self.provider.get_quote(
                                    stock_symbol
                                ).name
                            except BotError:
                                pass
                        analysis_topic = f"{stock_symbol} — {company_name}"
                        items = self.news_service.stock_headlines(
                            stock_symbol,
                            company_name,
                        )
                    else:
                        items = self.news_service.headlines(topic)
                except BotError as exc:
                    LOG.warning("News RSS unavailable: %s", exc)
                    items = []
                gemini = (
                    self.scanner.gemini
                    if self.scanner is not None
                    else build_gemini_analyzer(self.usage_store)
                )
                analysis = gemini.analyze_news(analysis_topic, items, macro)
                return format_news_report(analysis_topic, items, macro, analysis)
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
                remaining = self._claim_research_slot()
                if remaining:
                    return self._research_cooldown_message(remaining)
                daily_limit = self._claim_daily_command("scan_commands", "/scan")
                if daily_limit:
                    return daily_limit
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
        research_command_cooldown = float(
            os.environ.get(
                "RESEARCH_COMMAND_COOLDOWN",
                str(DEFAULT_RESEARCH_COMMAND_COOLDOWN),
            )
        )
        gemini_daily_budget = int(
            os.environ.get("GEMINI_DAILY_BUDGET", DEFAULT_GEMINI_DAILY_BUDGET)
        )
        deep_daily_limit = int(
            os.environ.get("DEEP_DAILY_LIMIT", DEFAULT_DEEP_DAILY_LIMIT)
        )
        scan_daily_limit = int(
            os.environ.get("SCAN_DAILY_LIMIT", DEFAULT_SCAN_DAILY_LIMIT)
        )
        news_daily_limit = int(
            os.environ.get("NEWS_DAILY_LIMIT", DEFAULT_NEWS_DAILY_LIMIT)
        )
        vnstock_daily_budget = int(
            os.environ.get("VNSTOCK_DAILY_BUDGET", DEFAULT_VNSTOCK_DAILY_BUDGET)
        )
        vnstock_requests_per_minute = int(
            os.environ.get(
                "VNSTOCK_REQUESTS_PER_MINUTE",
                DEFAULT_VNSTOCK_REQUESTS_PER_MINUTE,
            )
        )
        vnstock_usage_ratio = float(
            os.environ.get("VNSTOCK_USAGE_RATIO", DEFAULT_VNSTOCK_USAGE_RATIO)
        )
        vnstock_error_cooldown = float(
            os.environ.get("VNSTOCK_ERROR_COOLDOWN", DEFAULT_VNSTOCK_ERROR_COOLDOWN)
        )
        vnstock_cache_ttl = float(
            os.environ.get("VNSTOCK_CACHE_TTL", DEFAULT_VNSTOCK_CACHE_TTL)
        )
        vimo_cache_ttl = float(
            os.environ.get("VIMO_CACHE_TTL", DEFAULT_VIMO_CACHE_TTL)
        )
        vimo_max_age_hours = float(
            os.environ.get("VIMO_MAX_AGE_HOURS", DEFAULT_VIMO_MAX_AGE_HOURS)
        )
        news_cache_ttl = float(
            os.environ.get("NEWS_CACHE_TTL", DEFAULT_NEWS_CACHE_TTL)
        )
        news_max_items = int(
            os.environ.get("NEWS_MAX_ITEMS", DEFAULT_NEWS_MAX_ITEMS)
        )
    except ValueError as exc:
        raise ValueError("Các biến timeout/score/limit phải là số.") from exc
    telegram = TelegramClient(token, timeout=max(10.0, poll_timeout + 10.0))
    usage_store = ApiUsageStore(
        data_dir / "api_usage.json",
        gemini_daily_budget=gemini_daily_budget,
        vnstock_daily_budget=vnstock_daily_budget,
        deep_daily_limit=deep_daily_limit,
        scan_daily_limit=scan_daily_limit,
        news_daily_limit=news_daily_limit,
    )
    yahoo_provider = YahooQuoteProvider(timeout=max(1.0, yahoo_timeout))
    provider = RoutedMarketDataProvider(
        fallback=yahoo_provider,
        usage_store=usage_store,
        api_key=os.environ.get("VNSTOCK_API_KEY", ""),
        sources=re.split(
            r"[\s,;]+",
            os.environ.get("VNSTOCK_SOURCES", DEFAULT_VNSTOCK_SOURCES),
        ),
        requests_per_minute=vnstock_requests_per_minute,
        usage_ratio=vnstock_usage_ratio,
        error_cooldown=vnstock_error_cooldown,
        cache_ttl=vnstock_cache_ttl,
    )
    gemini = build_gemini_analyzer(usage_store)
    macro_client = MacroContextClient(
        url=os.environ.get("VIMO_LATEST_URL", DEFAULT_VIMO_LATEST_URL),
        timeout=max(2.0, yahoo_timeout),
        cache_ttl=vimo_cache_ttl,
        max_age_hours=vimo_max_age_hours,
    )
    news_service = NeutralNewsService(
        timeout=max(2.0, yahoo_timeout),
        cache_ttl=news_cache_ttl,
        max_items=news_max_items,
    )
    symbols = parse_symbols(os.environ.get("VN100_SYMBOLS"))
    return BotApplication(
        telegram=telegram,
        provider=provider,
        store=WatchlistStore(data_dir / "watchlists.json"),
        signal_store=SignalStore(data_dir / "signal_state.json"),
        usage_store=usage_store,
        macro_client=macro_client,
        news_service=news_service,
        scanner=DeepSignalScanner(
            provider=provider,
            gemini=gemini,
            symbols=symbols,
            macro_client=macro_client,
            min_score=min_signal_score,
            max_per_scan=max_signals_per_scan,
            max_workers=scan_workers,
        ),
        monthly_signal_limit=monthly_signal_limit,
        signal_cooldown_days=signal_cooldown_days,
        research_command_cooldown=research_command_cooldown,
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
