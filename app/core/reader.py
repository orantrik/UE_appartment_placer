import pandas as pd


def read_file(path: str) -> pd.DataFrame:
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
        df = df.dropna(how="all")
    else:
        df = pd.read_excel(path)
        df = df.dropna(how="all")
    df.columns = [str(c) for c in df.columns]

    # Normalize "/" entrance values to "0" so they form a valid calibration key
    for col in df.columns:
        if col.strip().lower() in ("entrance", "כניסה"):
            df[col] = df[col].apply(
                lambda v: "0" if str(v).strip() == "/" else v
            )
            break

    return df.reset_index(drop=True)
