"""ssh_channel 通/断与闲/忙状态推断。"""

from __future__ import annotations

import unittest

from services.ssh_channel_service import get_channel_session_state


class TestSshChannelSessionState(unittest.TestCase):
    def test_disconnected_when_db_closed(self):
        st = get_channel_session_state(999999, db_status="closed", host_type="Linux")
        self.assertFalse(st["connected"])
        self.assertFalse(st["memory_connected"])
        self.assertEqual(st["session_state"], "disconnected")
        self.assertFalse(st.get("can_send"))

    def test_disconnected_when_db_open_but_no_memory(self):
        st = get_channel_session_state(999998, db_status="open", host_type="Linux")
        self.assertFalse(st["connected"])
        self.assertFalse(st["memory_connected"])
        self.assertEqual(st["disconnect_reason"], "memory_disconnected")


if __name__ == "__main__":
    unittest.main()
