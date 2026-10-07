"""Isolated Google Cloud Translation Basic v2 diagnostic."""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from config import ConfigurationError, configure_logging, load_config
from preferences import UserPreferences
from providers.google_translate import GoogleTranslateBasicV2
from providers.translate import TranslationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_args(languages_config: dict[str, object]) -> argparse.Namespace:
    """Parse isolated translation diagnostic arguments."""
    parser = argparse.ArgumentParser(
        description="Translate one text with Google Cloud Translation Basic v2."
    )
    parser.add_argument(
        "text",
        nargs="?",
        default="Hello, this is an isolated translation test.",
        help="Text to translate (default: an English diagnostic phrase).",
    )
    parser.add_argument(
        "--source-language",
        default=languages_config["remote"],
        help="Source language code.",
    )
    parser.add_argument(
        "--target-language",
        default=languages_config["agent"],
        help="Target language code.",
    )
    return parser.parse_args()


async def run_translation(
    args: argparse.Namespace,
    translation_config: dict[str, object],
    api_key: str,
) -> None:
    """Perform one real Basic v2 request and report its result."""
    translator = GoogleTranslateBasicV2(
        api_key=api_key,
        endpoint=str(translation_config["endpoint"]),
        text_format=str(translation_config["format"]),
    )
    try:
        translator.connect()
        logging.info("Isolated Google Cloud Translation Basic v2 test")
        logging.info("Authentication: API key via x-goog-api-key header")
        logging.info("Languages: %s -> %s", args.source_language, args.target_language)
        logging.info("Source: %s", args.text)
        result = await translator.translate(
            args.text,
            source_language=args.source_language,
            target_language=args.target_language,
        )
        logging.info("Translation: %s", result.translated_text)
        logging.info("Latency: %.0f ms", result.latency_seconds * 1000)
        logging.info("Translation test completed successfully")
    finally:
        await translator.close()


def load_api_key() -> str:
    """Load the development credential without logging its value."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if not api_key:
        raise TranslationError(
            "GOOGLE_TRANSLATE_API_KEY is missing. Add it to the project .env file."
        )
    return api_key


def main() -> int:
    """Run the isolated translation diagnostic."""
    configure_logging()
    try:
        config = load_config(PROJECT_ROOT / "config" / "default.toml")
        translation_config = config["translation"]
        assert isinstance(translation_config, dict)
        languages_config = UserPreferences(
            PROJECT_ROOT / "config" / "preferences.json"
        ).participant_languages
        args = parse_args(languages_config)
        api_key = load_api_key()
        asyncio.run(run_translation(args, translation_config, api_key))
    except KeyboardInterrupt:
        logging.warning("Translation test interrupted by the user")
        return 130
    except (ConfigurationError, TranslationError, OSError, ValueError) as error:
        logging.error("Translation test failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
