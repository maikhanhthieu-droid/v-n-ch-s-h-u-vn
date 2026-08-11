import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from bot import AssistantConversationStore, BotApplication, WatchlistStore


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
