import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_core as ac  # noqa: E402


REVIEW_OK = "📥 1. Minimalist Correction\nThe protein increased by adding the drug.\n\n🔍 2. Polished Academic English\nThe protein increased upon addition of the drug."


class CleanSource(unittest.TestCase):
    def test_removes_object_replacement_char(self):
        self.assertEqual(ac.clean_source("请￼记录"), "请记录")

    def test_collapses_spaces_left_behind(self):
        self.assertEqual(ac.clean_source("by ￼ PPD in the US"), "by PPD in the US")
        self.assertEqual(ac.clean_source("a \t￼ b"), "a b")

    def test_keeps_other_text_and_newlines_untouched(self):
        text = "第一行\n\n第二行  两个空格"
        self.assertEqual(ac.clean_source(text), text)

    def test_newlines_are_not_merged(self):
        self.assertEqual(ac.clean_source("a￼\n￼b"), "a\nb")


class CheckChinese(unittest.TestCase):
    def test_chinese_ok(self):
        ac.check_chinese("受试者于访视 1 采集样本")

    def test_chinese_with_drug_names_ok(self):
        ac.check_chinese("给予 pembrolizumab 200 mg 静脉输注")

    def test_english_rejected(self):
        with self.assertRaises(ac.UnsupportedInputError):
            ac.check_chinese("Subjects will be dosed at visit 1.")

    def test_digits_only_rejected(self):
        with self.assertRaises(ac.UnsupportedInputError):
            ac.check_chinese("12345")


class ParseReview(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(ac.parse_review(REVIEW_OK), ("The protein increased by adding the drug.", "The protein increased upon addition of the drug."))

    def test_without_emoji_and_with_markdown_headings(self):
        raw = "**1. Minimalist Correction**\nA\n\n## 2. Polished Academic English\nB"
        self.assertEqual(ac.parse_review(raw), ("A", "B"))

    def test_multiline_sections_keep_line_breaks(self):
        raw = "📥 1. Minimalist Correction\nline1\nline2\n\n🔍 2. Polished Academic English\nL1\nL2"
        self.assertEqual(ac.parse_review(raw), ("line1\nline2", "L1\nL2"))

    def test_wrapped_in_code_fence(self):
        raw = "```\n" + REVIEW_OK + "\n```"
        self.assertEqual(ac.parse_review(raw)[1], "The protein increased upon addition of the drug.")

    def test_heading_text_inside_a_sentence_is_not_a_heading(self):
        raw = "📥 1. Minimalist Correction\nSee 2. Polished Academic English later.\n\n🔍 2. Polished Academic English\nB"
        self.assertEqual(ac.parse_review(raw), ("See 2. Polished Academic English later.", "B"))

    def test_missing_second_heading(self):
        with self.assertRaises(ac.ReviewFormatError):
            ac.parse_review("📥 1. Minimalist Correction\nonly one")

    def test_wrong_order(self):
        with self.assertRaises(ac.ReviewFormatError):
            ac.parse_review("🔍 2. Polished Academic English\nB\n📥 1. Minimalist Correction\nA")

    def test_empty_section(self):
        with self.assertRaises(ac.ReviewFormatError):
            ac.parse_review("📥 1. Minimalist Correction\n\n🔍 2. Polished Academic English\nB")


class FakeClient:
    """按调用顺序返回预设内容，并记下收到的消息。"""

    calls = []
    replies = []

    def __init__(self, **kwargs):
        pass

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, model, temperature, messages):
        FakeClient.calls.append({"model": model, "temperature": temperature, "messages": messages})
        reply = FakeClient.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        choice = mock.Mock()
        choice.message.content = reply
        choice.finish_reason = "stop"
        return mock.Mock(choices=[choice])


class RunTwoStep(unittest.TestCase):
    def setUp(self):
        FakeClient.calls, FakeClient.replies = [], []
        patcher = mock.patch.object(ac, "OpenAI", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_happy_path_and_messages(self):
        FakeClient.replies = ["Subjects take the drug.", REVIEW_OK]
        out = ac.run_two_step("受试者￼服药", "m", "k", "http://x", "TRANSLATOR RULES", "REVIEWER RULES")
        self.assertEqual(out[0], "Subjects take the drug.")
        self.assertEqual(out[1], "The protein increased by adding the drug.")
        self.assertEqual(out[2], "The protein increased upon addition of the drug.")
        self.assertEqual(out[3], "中译英")
        first, second = FakeClient.calls
        self.assertEqual(first["messages"], [{"role": "system", "content": "TRANSLATOR RULES"}, {"role": "user", "content": "受试者服药"}])
        # 审校只收到英文译文，收不到原文
        self.assertEqual(second["messages"], [{"role": "system", "content": "REVIEWER RULES"}, {"role": "user", "content": "Subjects take the drug."}])

    def test_empty_system_prompt_sends_no_system_message(self):
        FakeClient.replies = ["T", REVIEW_OK]
        ac.run_two_step("受试者服药", "m", "k", "http://x", "", "  ")
        for call in FakeClient.calls:
            self.assertEqual([m["role"] for m in call["messages"]], ["user"])

    def test_english_input_makes_no_call(self):
        with self.assertRaises(ac.UnsupportedInputError):
            ac.run_two_step("Subjects take the drug.", "m", "k", "http://x", "T", "R")
        self.assertEqual(FakeClient.calls, [])

    def test_step1_failure(self):
        FakeClient.replies = [RuntimeError("boom")]
        with self.assertRaises(ac.StepError) as cm:
            ac.run_two_step("受试者服药", "m", "k", "http://x", "T", "R")
        self.assertEqual(cm.exception.step, 1)
        self.assertEqual(cm.exception.partial, {})

    def test_step2_failure_keeps_translation(self):
        FakeClient.replies = ["Subjects take the drug.", RuntimeError("boom")]
        with self.assertRaises(ac.StepError) as cm:
            ac.run_two_step("受试者服药", "m", "k", "http://x", "T", "R")
        self.assertEqual(cm.exception.step, 2)
        self.assertEqual(cm.exception.partial, {"translation": "Subjects take the drug."})

    def test_unparseable_review_keeps_raw_output(self):
        FakeClient.replies = ["Subjects take the drug.", "Sure! Here is the text."]
        with self.assertRaises(ac.StepError) as cm:
            ac.run_two_step("受试者服药", "m", "k", "http://x", "T", "R")
        self.assertIsInstance(cm.exception.cause, ac.ReviewFormatError)
        self.assertEqual(cm.exception.partial["review_raw"], "Sure! Here is the text.")

    def test_empty_completion(self):
        FakeClient.replies = ["   "]
        with self.assertRaises(ac.StepError) as cm:
            ac.run_two_step("受试者服药", "m", "k", "http://x", "T", "R")
        self.assertIsInstance(cm.exception.cause, ac.EmptyCompletionError)


if __name__ == "__main__":
    unittest.main()
