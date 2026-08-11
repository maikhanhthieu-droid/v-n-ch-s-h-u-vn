import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from bot import (
    AssistantConversationStore,
    BotApplication,
    FundamentalSnapshot,
    Quote,
    WatchlistStore,
)


class AssistantPanelTests(TestCase):
    def test_store_persists_and_caps_response_at_eighty_words(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.json"
            store = AssistantConversationStore(path)
            store.start(7, "fpt", "quant evidence")
            response = " ".join(f"w{i}" for i in range(100))

            session = store.add_response(7, "deepseek", response)

            self.assertEqual(session["symbol"], "FPT")
            self.assertEqual(len(session["responses"]["deepseek"].split()), 80)
            restored = AssistantConversationStore(path).get(7)
            self.assertEqual(restored["responses"], session["responses"])

    def test_telegram_commands_share_prior_views_and_save_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssistantConversationStore(Path(directory) / "conversations.json")
            store.start(7, "FPT", "score 80; stop 90; target 110")
            analyzer = Mock()
            analyzer.summarize_assistant_panel.return_value = "TRẠNG THÁI: CHỜ"
            scanner = Mock()
            scanner.gemini = analyzer
            app = BotApplication(
                telegram=Mock(),
                provider=Mock(),
                store=WatchlistStore(Path(directory) / "watchlists.json"),
                scanner=scanner,
                conversation_store=store,
            )

            status = app.handle_text(
                "/ai_add deepseek CHỜ hỗ trợ; cảnh báo thanh khoản suy yếu", 7
            )
            self.assertIn("DeepSeek", status)
            prompt = app.handle_text("/ai_prompt glm", 7)
            self.assertIn("thanh khoản", prompt)
            self.assertIn("VÙNG MUA", prompt)
            self.assertIn("VÙNG BÁN/CHỐT LỜI", prompt)
            self.assertIn("STOP", prompt)
            summary = app.handle_text("/ai_summary", 7)
            self.assertIn("TRẠNG THÁI: CHỜ", summary)
            analyzer.summarize_assistant_panel.assert_called_once()

    def test_deep_opens_panel_and_preserves_successful_views_when_one_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            conversation_store = AssistantConversationStore(
                Path(directory) / "conversations.json"
            )
            provider = Mock()
            provider.get_quote.return_value = Quote("FPT", "FPT", 100.0, 99.0)
            provider.get_history.return_value = []
            provider.get_fundamentals.return_value = FundamentalSnapshot("FPT", "FPT")
            scanner = Mock()
            scanner.render_signal.return_value = "deep result"
            scanner.assistant_views_for.return_value = {
                "glm": "CHỜ vùng quant; cơ bản ổn",
                "gemini": "THEO DÕI dòng tiền",
            }
            app = BotApplication(
                telegram=Mock(),
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
                scanner=scanner,
                conversation_store=conversation_store,
                research_command_cooldown=0,
            )

            result = app.handle_text("/deep FPT", 7)

            self.assertIn("Tự động chuyển sang panel bổ sung", result)
            self.assertIn("Thiếu: DeepSeek", result)
            self.assertIn("<b>GLM</b> ✅", result)
            self.assertIn("<b>Gemini</b> ✅", result)
            self.assertIn("<b>DeepSeek</b> ⏳ chưa nhận", result)
            session = conversation_store.get(7)
            self.assertEqual(set(session["responses"]), {"glm", "gemini"})

    def test_deep_does_not_open_panel_when_all_three_views_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Mock()
            provider.get_quote.return_value = Quote("FPT", "FPT", 100.0, 99.0)
            provider.get_history.return_value = []
            provider.get_fundamentals.return_value = FundamentalSnapshot("FPT", "FPT")
            scanner = Mock()
            scanner.render_signal.return_value = "deep result"
            scanner.assistant_views_for.return_value = {
                "glm": "ok",
                "deepseek": "ok",
                "gemini": "ok",
            }
            app = BotApplication(
                telegram=Mock(),
                provider=provider,
                store=WatchlistStore(Path(directory) / "watchlists.json"),
                scanner=scanner,
                conversation_store=AssistantConversationStore(
                    Path(directory) / "conversations.json"
                ),
                research_command_cooldown=0,
            )

            self.assertEqual(app.handle_text("/deep FPT", 7), "deep result")
