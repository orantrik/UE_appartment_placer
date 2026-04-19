from dataclasses import dataclass, field
import pandas as pd


@dataclass
class AppData:
    df: object = None           # pd.DataFrame | None
    file_path: str = ""

    # Required column mappings: internal_key -> excel_column_name
    required_mappings: dict = field(default_factory=dict)
    # Keys: "building", "entrance", "floor", "apt_id", "direction"

    # Extra mappings: list of (excel_col_name, ue_variable_name)
    extra_mappings: list = field(default_factory=list)

    # Spacing in cm
    building_spacing_cm: int = 10000
    floor_height_cm: int = 300
    direction_spacing_cm: int = 1000
    entrance_offset_cm: int = 500
    stack_offset_cm: int = 200

    # Per-transition floor-gap overrides, keyed by the "from" floor index.
    # e.g. {1: 900} means the gap from floor 1 to floor 2 is 900 cm instead
    # of the global floor_height_cm. Every floor above the override shifts
    # up by (override - floor_height_cm). Floors not listed use the default.
    floor_gaps_cm: dict = field(default_factory=dict)

    # Default pitch (in the user's "intuitive" convention: positive = camera
    # tilts UP) applied to every apt-type polygon whose per-polygon
    # spring_arm entry does not carry its own pitch_deg override. Drives
    # both the 2D preview label and the baked DEFAULT_SA_PITCH_DEG constant
    # in the generated UE script. Internally inverted (-value) before being
    # fed to SpringArmComponent.Rotation because UE's SpringArm pitch
    # rotates the arm (not the camera), so + UE-pitch = camera descends.
    default_spring_arm_pitch_deg: float = 0.0

    blueprint_path: str = "/Game/BP_Apartment.BP_Apartment_C"

    # Free-text project name. Used as a prefix for the unique
    # /Game/ApartmentMeshes/<ProjectName>_<hash>/ folder created in UE
    # by the generated volume script.
    project_name: str = ""

    # Floor plan calibration (populated by PlanCanvas)
    # {scale_px_per_m, north_angle_deg, entrances: [{building_id, entrance_id,
    #   polygon_img, center_img, world_x_m, world_y_m}]}
    calibration: dict = field(default_factory=dict)
