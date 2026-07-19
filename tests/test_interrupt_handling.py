"""Tests for visible and repeat-safe Ctrl+C handling."""

import asyncio
import signal
import unittest
from unittest.mock import patch

import main as application_main


class InterruptHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_c_is_traced_and_repeated_press_does_not_force_exit(self) -> None:
        application_started = asyncio.Event()

        async def fake_run_application(_args: object) -> None:
            application_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.05)

        async def send_interrupts() -> None:
            await application_started.wait()
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0.01)
            signal.raise_signal(signal.SIGINT)

        with (
            patch.object(application_main, "run_application", fake_run_application),
            self.assertLogs(application_main.__name__, level="INFO") as captured,
        ):
            sender = asyncio.create_task(send_interrupts())
            interrupted = await asyncio.wait_for(
                application_main.run_application_with_interrupt_trace(object()),
                timeout=1.0,
            )
            await sender

        self.assertTrue(interrupted)
        messages = "\n".join(captured.output)
        self.assertIn("event=application_shutdown_requested reason=user_interrupt", messages)
        self.assertEqual(messages.count("event=application_shutdown_requested"), 1)


if __name__ == "__main__":
    unittest.main()
