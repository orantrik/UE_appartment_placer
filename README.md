# UE Apartment Placer

A desktop tool that imports apartment data from Excel/CSV and generates a self-contained Unreal Engine Python script to spawn and place blueprint actors in the viewport.

## Features

- Import Excel (`.xlsx`) or CSV files
- Map table columns to required fields: Building, Entrance, Floor, Apartment ID, Direction
- Add extra variable mappings (Excel column → UE blueprint variable)
- Spacing rulers (sliders) for Building Spacing, Floor Height, Direction Spacing, Entrance Offset, Stack Offset
- Generates a ready-to-run UE Python script with all data embedded
- Save or copy the script to clipboard

## Placement Logic

| Axis | Controlled by |
|------|--------------|
| X    | Building (100 m apart by default) + Entrance offset |
| Y    | Apartment index within floor, sorted by direction (North → +Y) |
| Z    | Floor number × Floor Height |

Direction values support Hebrew (`צפון`, `דרום`, `מזרח`, `מערב`) and English (`North`, `South`, `East`, `West`). Compound directions are averaged.

## Setup

```bash
pip install -r requirements.txt
python main.py
```

## Usage

1. **Browse** — load an Excel or CSV file
2. **Field Mapping tab** — assign columns to Building, Floor, Direction, etc.
3. **Spacing tab** — adjust spacing values with sliders
4. **Script tab** — set Blueprint path, click **Generate Script**
5. Copy or save the `.py` file
6. In Unreal Engine → Output Log → Python:
   ```python
   exec(open(r"C:/path/to/spawn_apartments.py").read())
   ```

## Requirements

- Python 3.10+
- PyQt6
- pandas
- openpyxl
