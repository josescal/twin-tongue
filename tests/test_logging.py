"""Tests for asynchronous contextual logging, rotation, and backpressure."""

import logging
from io import StringIO
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest

from config import (
    LOG_PIPELINE,
    configure_logging,
    get_dropped_log_count,
    set_log_pipeline,
    shutdown_logging,
)


class AsyncLoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        shutdown_logging()
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            handler.close()

    def test_message_is_written_to_console_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "twin-tongue.log"
            console = StringIO()
            configure_logging(log_path=log_path, console_stream=console)

            logging.getLogger("dual-output-test").info("visible in both destinations")
            shutdown_logging()

            self.assertIn("visible in both destinations", console.getvalue())
            written = log_path.read_text("utf-8")
            self.assertIn("[app] visible in both destinations", written)
            self.assertNotIn("run_id=", written)
            self.assertIn("visible in both destinations", written)

    def test_pipeline_context_is_preserved_across_listener_thread(self) -> None:
        console = StringIO()
        configure_logging(log_path=None, console_stream=console)
        token = set_log_pipeline("agent_to_remote")
        try:
            logging.getLogger("context-thread-test").info("context survives queue")
        finally:
            LOG_PIPELINE.reset(token)

        shutdown_logging()

        self.assertIn("[agent_to_remote] context survives queue", console.getvalue())
        self.assertNotIn("run_id=", console.getvalue())
        self.assertIn("context survives queue", console.getvalue())

    def test_third_party_info_logs_are_suppressed(self) -> None:
        console = StringIO()
        configure_logging(log_path=None, console_stream=console)

        logging.getLogger("httpx").info("request details must not leak")
        logging.getLogger("httpx").warning("provider warning remains visible")
        shutdown_logging()

        self.assertNotIn("request details must not leak", console.getvalue())
        self.assertIn("provider warning remains visible", console.getvalue())

    def test_file_rotates_and_keeps_configured_maximum_number_of_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "twin-tongue.log"
            configure_logging(
                log_path=log_path,
                log_size_mb=250 / (1024 * 1024),
                log_max_files=3,
                console_stream=StringIO(),
            )

            logger = logging.getLogger("rotation-test")
            for index in range(30):
                logger.info("rotation record %02d %s", index, "x" * 80)
            shutdown_logging()

            self.assertTrue(log_path.is_file())
            self.assertTrue(log_path.with_name("twin-tongue.log.1").is_file())
            self.assertLessEqual(len(list(log_path.parent.glob("twin-tongue.log*"))), 3)

    def test_slow_output_does_not_block_logging_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            release_output = Event()
            output_started = Event()

            class SlowConsole(StringIO):
                def write(self, value: str) -> int:
                    output_started.set()
                    release_output.wait(2.0)
                    return super().write(value)

            log_path = Path(temporary_directory) / "twin-tongue.log"
            configure_logging(log_path=log_path, console_stream=SlowConsole())

            started_at = time.perf_counter()
            logging.getLogger("non-blocking-test").info("slow output")
            elapsed = time.perf_counter() - started_at

            self.assertLess(elapsed, 0.1)
            self.assertTrue(output_started.wait(1.0))
            release_output.set()
            shutdown_logging()
            self.assertIn("slow output", log_path.read_text("utf-8"))

    def test_full_queue_drops_records_without_blocking_and_reports_total(self) -> None:
        release_output = Event()
        output_started = Event()

        class BlockedConsole(StringIO):
            def write(self, value: str) -> int:
                output_started.set()
                release_output.wait(2.0)
                return super().write(value)

        console = BlockedConsole()
        configure_logging(log_path=None, log_queue_capacity=1, console_stream=console)
        logger = logging.getLogger("queue-capacity-test")
        logger.info("first record blocks the listener")
        self.assertTrue(output_started.wait(1.0))
        logger.info("second record fills the queue")

        started_at = time.perf_counter()
        logger.info("third record is discarded")
        elapsed = time.perf_counter() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertEqual(get_dropped_log_count(), 1)
        release_output.set()
        self.assertEqual(shutdown_logging(), 1)
        self.assertIn("Discarded 1 log record(s)", console.getvalue())


if __name__ == "__main__":
    unittest.main()
