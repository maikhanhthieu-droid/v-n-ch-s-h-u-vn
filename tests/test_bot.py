import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import (  # noqa: E402
    BotApplication,
    BotError,
    Quote,
    WatchlistStore,
    format_quote,
    normalize_symbol,
    yahoo_symbol,
)


class FakeProvider:
    def __init__(self):
        self.calls = []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        return Quote(
            symbol=symbol,
            name=f"{symbol} Company",
            price=100.0,
            previous_close=95.0,
        )


class BotTests(unittest.TestCase):
    def test_normalize_symbol(self):
        self.assertEqual(normalize_symbol(" fpt.vn "), "FPT")
        with self.assertRaises(ValueError):
            normalize_symbol("FPT-ABC")

    def test_yahoo_symbol(self):
        self.assertEqual(yahoo_symbol("FPT"), "FPT.VN")
        self.assertEqual(yahoo_symbol("VNINDEX"), "^VNINDEX.VN")

    def test_format_quote_escapes_name(self):
        quote = Quote("FPT", "<unsafe>", 100, 95)
        rendered = format_quote(quote)
        self.assertIn("&lt;unsafe&gt;", rendered)
        self.assertIn("+5.26%", rendered)

    def test_watchlist_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watchlists.json"
            first = WatchlistStore(path)
            self.assertTrue(first.add("42", "FPT")[0])
            second = WatchlistStore(path)
            self.assertEqual(second.get("42"), ["FPT"])
            self.assertTrue(second.remove("42", "FPT")[0])
            self.assertEqual(second.get("42"), [])

    def test_command_router(self):
        with tempfile.TemporaryDirectory() as directory:
            telegram = Mock()
            provider = FakeProvider()
            app = BotApplication(
                telegram=telegram,
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
            )
            self.assertIn("VN Equity Bot", app.handle_text("/start", 1))
            self.assertIn("FPT", app.handle_text("/add FPT", 1))
            self.assertIn("FPT", app.handle_text("/watchlist", 1))
            self.assertIn("100.00", app.handle_text("/quote FPT", 1))
            self.assertEqual(provider.calls, ["FPT"])
            app.handle_update(
                {"update_id": 1, "message": {"chat": {"id": 1}, "text": "/ping"}}
            )
            telegram.send_message.assert_called_once_with(1, "pong ✅")

    def test_provider_errors_are_user_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            telegram = Mock()
            provider = Mock()
            provider.get_quote.side_effect = BotError("Không lấy được dữ liệu thị trường lúc này.")
            app = BotApplication(
                telegram=telegram,
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
            )
            self.assertIn("Không lấy được", app.handle_text("/quote VNM", 1))


if __name__ == "__main__":
    unittest.main()
