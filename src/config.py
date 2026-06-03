import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MESHY_API_KEY = os.environ.get("MESHY_API_KEY")
# Chiave Meshy di fallback: usata automaticamente se la principale esaurisce i
# crediti (HTTP 402 / errore credits sulla POST di creazione task). Opzionale.
MESHY_API_KEY_FALLBACK = os.environ.get("MESHY_API_KEY_FALLBACK")

MESHY_BASE = "https://api.meshy.ai/openapi/v1"
MESHY_TEST_KEY = "msy_dummy_api_key_for_test_mode_12345678"


def meshy_api_keys() -> list[str]:
    """Chiavi Meshy in ordine di preferenza: principale poi fallback. Deduplicata,
    solo voci non vuote. Il client le prova in sequenza, passando alla successiva
    se quella corrente è a crediti esauriti."""
    keys: list[str] = []
    for k in (MESHY_API_KEY, MESHY_API_KEY_FALLBACK):
        if k and k not in keys:
            keys.append(k)
    return keys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"

ANTHROPIC_MODEL = "claude-opus-4-7"
MESHY_IMAGE_MODEL = "nano-banana-pro"
MESHY_3D_MODEL = "meshy-6"

# Default per il pre-step di pulizia reference (image-to-image) prima della
# generazione. La UI può sovrascriverlo per-sessione; questo è il fallback per
# i path non-UI (es. CLI). Costa crediti extra → default off.
MESHY_CLEAN_REFERENCES = os.environ.get("MESHY_CLEAN_REFERENCES", "").lower() in (
    "1", "true", "yes", "on",
)


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
