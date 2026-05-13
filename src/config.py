import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MESHY_API_KEY = os.environ.get("MESHY_API_KEY")

MESHY_BASE = "https://api.meshy.ai/openapi/v1"
MESHY_TEST_KEY = "msy_dummy_api_key_for_test_mode_12345678"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"

ANTHROPIC_MODEL = "claude-opus-4-7"
MESHY_IMAGE_MODEL = "nano-banana-pro"
MESHY_3D_MODEL = "meshy-6"


def assert_keys_present() -> None:
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not MESHY_API_KEY:
        missing.append("MESHY_API_KEY")
    if missing:
        raise RuntimeError(
            f"Variabili d'ambiente mancanti: {', '.join(missing)}. "
            "Copia .env.example in .env e compila le key."
        )


def is_test_mode() -> bool:
    return MESHY_API_KEY == MESHY_TEST_KEY
