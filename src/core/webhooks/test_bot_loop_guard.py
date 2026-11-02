import unittest
from unittest import mock

from core.webhooks import bot_loop_guard
from core.webhooks.bot_loop_guard import is_recent_bot_reply, remember_bot_reply, reply_budget_ok


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def exists(self, key):
        return int(key in self.store)


class BotReplyMemoryTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch("utilities.rate_limit._redis", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_reply_echoed_back_by_another_inbox_is_recognised(self):
        remember_bot_reply("Hey, welcome to AutoBus! What are you looking for today?")
        self.assertTrue(is_recent_bot_reply("hey, welcome to AutoBus!  What are you looking for today?"))

    def test_people_messaging_from_a_connected_account_still_get_answers(self):
        remember_bot_reply("Hey, welcome to AutoBus! What are you looking for today?")
        self.assertFalse(is_recent_bot_reply("Hi, do you sell shea butter?"))

    def test_empty_text_is_never_treated_as_a_bot_reply(self):
        remember_bot_reply("")
        self.assertFalse(is_recent_bot_reply(""))

    def test_redis_outage_lets_messages_through(self):
        with mock.patch("utilities.rate_limit._redis", return_value=None):
            remember_bot_reply("hello")
            self.assertFalse(is_recent_bot_reply("hello"))


class ReplyBudgetTests(unittest.TestCase):
    def test_budget_follows_the_rate_counter(self):
        with mock.patch("utilities.rate_limit.hit", return_value=(True, 3, 0)):
            self.assertTrue(reply_budget_ok("m:ig:1"))
        with mock.patch("utilities.rate_limit.hit", return_value=(False, 0, 120)):
            self.assertFalse(reply_budget_ok("m:ig:1"))

    def test_budget_is_tunable(self):
        with mock.patch.dict("os.environ", {"AUTO_REPLY_BURST_LIMIT": "4"}):
            self.assertEqual(bot_loop_guard._env_int("AUTO_REPLY_BURST_LIMIT", 15), 4)


if __name__ == "__main__":
    unittest.main()
