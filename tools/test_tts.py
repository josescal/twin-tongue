"""Isolated ElevenLabs text-to-speech diagnostic."""

import argparse
import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
import wave

from dotenv import load_dotenv

from config import ConfigurationError, configure_logging, load_config
from providers.elevenlabs_tts import ElevenLabsTTS
from providers.tts import TTSError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://api.elevenlabs.io"


def parse_args(
    tts_config: dict[str, object],
    languages: dict[str, object],
    default_voice_gender: str,
) -> argparse.Namespace:
    """Parse isolated TTS diagnostic arguments."""
    parser = argparse.ArgumentParser(description="Synthesize one WAV file with ElevenLabs TTS.")
    parser.add_argument(
        "text",
        nargs="?",
        default="Hola, esta es una prueba aislada de síntesis de voz.",
    )
    parser.add_argument("--language", default=languages["agent"])
    parser.add_argument(
        "--voice-gender",
        choices=("male", "female"),
        default=default_voice_gender,
    )
    parser.add_argument(
        "--voice-id",
        default=None,
        help="Override the configured voice for the selected language.",
    )
    parser.add_argument("--speed", type=float, default=tts_config["speed"])
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "tts_test.wav",
    )
    return parser.parse_args()


async def run_tts(
    args: argparse.Namespace,
    tts_config: dict[str, object],
    api_key: str,
    base_url: str,
) -> None:
    """Synthesize one phrase and save it as a standard WAV file."""
    default_voice_id = str(args.voice_id or tts_config["voice_id"])
    language_voices = (
        {}
        if args.voice_id
        else {
            str(language): {
                str(gender): str(voice_id)
                for gender, voice_id in dict(voices).items()
            }
            for language, voices in dict(
                tts_config.get("language_voices", {})
            ).items()
        }
    )
    provider = ElevenLabsTTS(
        api_key=api_key,
        voice_id=default_voice_id,
        model=str(tts_config["model"]),
        output_format=str(tts_config["output_format"]),
        sample_rate=int(tts_config["sample_rate"]),
        speed=args.speed,
        base_url=base_url,
        language_voices=language_voices,
        language_models={
            str(language): str(model)
            for language, model in dict(
                tts_config.get("language_models", {})
            ).items()
        },
    )
    try:
        provider.connect()
        logging.info("Isolated ElevenLabs TTS test")
        logging.info("Language: %s", args.language)
        logging.info("Voice gender: %s", args.voice_gender)
        logging.info(
            "Voice ID: %s",
            language_voices.get(args.language, {}).get(
                args.voice_gender, default_voice_id
            ),
        )
        logging.info("Speed: %.2f", args.speed)
        logging.info("Text: %s", args.text)
        result = await provider.synthesize(
            args.text, args.language, args.voice_gender
        )
        output_path = args.output.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(result.channels)
            wav_file.setsampwidth(result.sample_width_bytes)
            wav_file.setframerate(result.sample_rate)
            wav_file.writeframes(result.audio)
        logging.info("WAV output: %s", output_path)
        logging.info("PCM bytes: %s", len(result.audio))
        logging.info("First-byte latency: %.0f ms", result.first_byte_latency_seconds * 1000)
        logging.info("Total latency: %.0f ms", result.total_latency_seconds * 1000)
        logging.info("TTS test completed successfully")
    finally:
        await provider.close()


def load_environment() -> tuple[str, str]:
    """Load ElevenLabs credentials without logging their values."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise TTSError("ELEVENLABS_API_KEY is missing. Add it to the project .env file.")
    base_url = os.getenv("ELEVENLABS_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise TTSError("ELEVENLABS_BASE_URL must be a valid HTTP or HTTPS URL.")
    return api_key, base_url


def main() -> int:
    """Run the isolated TTS diagnostic."""
    configure_logging()
    try:
        config = load_config(PROJECT_ROOT / "config" / "default.toml")
        tts_config = config["tts"]
        pipelines = config["pipelines"]
        assert isinstance(tts_config, dict)
        assert isinstance(pipelines, dict)
        remote_pipeline = pipelines["remote_to_agent"]
        assert isinstance(remote_pipeline, dict)
        args = parse_args(
            tts_config,
            {"agent": "es"},
            str(remote_pipeline["voice_gender"]),
        )
        api_key, base_url = load_environment()
        asyncio.run(run_tts(args, tts_config, api_key, base_url))
    except KeyboardInterrupt:
        logging.warning("TTS test interrupted by the user")
        return 130
    except (ConfigurationError, TTSError, OSError, ValueError) as error:
        logging.error("TTS test failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
