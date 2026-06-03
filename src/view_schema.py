"""Schema viste centralizzato per la pipeline Oggetti (e Portraits).

UNICA fonte di verità per: quante viste, i loro nomi, la loro posizione nella
griglia composita, il frammento di prompt che descrive l'angolo, e le pose di
render per i backend di build (Blender `view_axis`, Fusion `camera`).

Prima questi dati erano cablati e duplicati in più punti (grid_utils.GRID_QUADRANTS,
blender_build.VIEW_NAMES/VIEW_AXIS, fusion_build.VIEW_NAMES/VIEW_CAMERA, i system
prompt e i template). Ora vivono qui e si propagano per riferimento.

Back-compat: ogni sessione salva il suo `view_schema` (per key) in metadata.json /
components.json. I consumer risolvono lo schema dalla sessione; se la key manca
(sessioni vecchie) ripiegano su `LEGACY_2x2` (le 4 viste front/three_quarter/
side/back), così gli output già su disco continuano a funzionare.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ViewSpec:
    """Una singola vista del soggetto in una griglia composita."""

    name: str           # slug usato nei filename e nei dict views (es. "front_3q")
    col: int            # colonna nella griglia (0-based)
    row: int            # riga nella griglia (0-based)
    label: str          # etichetta UI (es. "Front ¾")
    prompt_angle: str   # descrizione dell'angolo per lo staging block (inglese)
    blender_axis: str   # hint bpy.ops.view3d.view_axis per il render di verifica
    fusion_camera: dict # posa camera Fusion: {view_dir, up, ortho}


@dataclass(frozen=True)
class ViewSchema:
    """Insieme ordinato di viste + geometria della griglia composita."""

    key: str            # identificatore stabile salvato in metadata
    cols: int           # colonne della griglia
    rows: int           # righe della griglia
    aspect_ratio: str   # aspect ratio da chiedere a Meshy (es. "2:1", "1:1")
    views: tuple[ViewSpec, ...] = field(default_factory=tuple)

    @property
    def names(self) -> list[str]:
        return [v.name for v in self.views]

    def __len__(self) -> int:
        return len(self.views)


# --- Oggetti: due viste 3/4 (front + rear), layout 1x2 orizzontale ----------
# Sostituisce il vecchio 2x2 con viste ortogonali pure (side 90° / back 180°),
# che perdevano profondità e producevano ricostruzioni piatte/incoerenti. Le due
# viste 3/4 catturano più informazione 3D per vista e sono più facili da mantenere
# coerenti tra loro per il modello image-to-3D.
OBJECT_VIEWS = ViewSchema(
    key="object_2x3q",
    cols=2,
    rows=1,
    # 16:9 (non 2:1): è il preset orizzontale supportato da Meshy nano-banana-pro
    # già usato in passato per il layout a due viste; ogni cella resta ~quasi
    # quadrata dopo lo split a metà.
    aspect_ratio="16:9",
    views=(
        ViewSpec(
            name="front_3q",
            col=0,
            row=0,
            label="Front ¾",
            prompt_angle=(
                "front three-quarter view: the object rotated ~35 degrees around "
                "the vertical axis so its front face and one flank are both visible, "
                "tilted ~10 degrees downward"
            ),
            blender_axis="FRONT",
            fusion_camera={"view_dir": [1, 1, -1], "up": [0, 0, 1], "ortho": False},
        ),
        ViewSpec(
            name="rear_3q",
            col=1,
            row=0,
            label="Rear ¾",
            prompt_angle=(
                "rear three-quarter view: the same object rotated ~215 degrees "
                "around the vertical axis so its back face and the opposite flank "
                "are both visible, same camera height and tilt"
            ),
            blender_axis="BACK",
            fusion_camera={"view_dir": [-1, -1, -1], "up": [0, 0, 1], "ortho": False},
        ),
    ),
)


# --- Legacy: 2x2 a 4 viste ortogonali (sessioni precedenti) ------------------
# Conserva ESATTAMENTE i valori storici di grid_utils.GRID_QUADRANTS +
# blender_build.VIEW_AXIS + fusion_build.VIEW_CAMERA, così le sessioni vecchie
# (senza `view_schema` in metadata) si risolvono qui e restano compatibili.
LEGACY_2x2 = ViewSchema(
    key="legacy_2x2",
    cols=2,
    rows=2,
    aspect_ratio="1:1",
    views=(
        ViewSpec("front", 0, 0, "Front",
                 "near-frontal view tilted ~15 degrees off axis",
                 "FRONT", {"view_dir": [0, 1, 0], "up": [0, 0, 1], "ortho": True}),
        ViewSpec("three_quarter", 1, 0, "3/4",
                 "three-quarter view, object rotated 45 degrees clockwise",
                 "FRONT", {"view_dir": [1, 1, -1], "up": [0, 0, 1], "ortho": False}),
        ViewSpec("side", 0, 1, "Side",
                 "pure 90-degree side profile, left flank toward the camera",
                 "RIGHT", {"view_dir": [-1, 0, 0], "up": [0, 0, 1], "ortho": True}),
        ViewSpec("back", 1, 1, "Back",
                 "pure 180-degree rear view",
                 "BACK", {"view_dir": [0, -1, 0], "up": [0, 0, 1], "ortho": True}),
    ),
)


# --- Portraits: invariato (2x2 a 4 viste) ------------------------------------
# La sezione Portraits non cambia: usa il proprio 2x2. È qui solo per riuso del
# cropping generico; gli angoli sono descritti nel PORTRAIT_GRID_SYSTEM_PROMPT.
PORTRAIT_VIEWS = ViewSchema(
    key="portrait_2x2",
    cols=2,
    rows=2,
    aspect_ratio="1:1",
    views=(
        ViewSpec("front", 0, 0, "Front", "front view", "FRONT",
                 {"view_dir": [0, 1, 0], "up": [0, 0, 1], "ortho": True}),
        ViewSpec("three_quarter", 1, 0, "3/4", "three-quarter view", "FRONT",
                 {"view_dir": [1, 1, -1], "up": [0, 0, 1], "ortho": False}),
        ViewSpec("side", 0, 1, "Side", "side profile", "RIGHT",
                 {"view_dir": [-1, 0, 0], "up": [0, 0, 1], "ortho": True}),
        ViewSpec("back", 1, 1, "Back", "back view", "BACK",
                 {"view_dir": [0, -1, 0], "up": [0, 0, 1], "ortho": True}),
    ),
)


SCHEMAS: dict[str, ViewSchema] = {
    s.key: s for s in (OBJECT_VIEWS, LEGACY_2x2, PORTRAIT_VIEWS)
}

# Schema corrente per le NUOVE sessioni Oggetti.
DEFAULT_OBJECT_SCHEMA = OBJECT_VIEWS


def get_schema(key: str | None) -> ViewSchema:
    """Ritorna lo schema per `key`, con fallback al legacy 4-viste se la key è
    assente/sconosciuta (sessioni Oggetti precedenti all'astrazione)."""
    if key and key in SCHEMAS:
        return SCHEMAS[key]
    return LEGACY_2x2


def resolve_object_schema(meta: dict) -> ViewSchema:
    """Risolve lo schema viste di una sessione Oggetti dal suo metadata/manifest.
    `meta` può essere il metadata.json della sessione o un components.json."""
    return get_schema((meta or {}).get("view_schema"))
