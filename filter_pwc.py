
import pandas as pd
from pathlib import Path

INPUT = Path("data/pwc/train-00000-of-00001.parquet")
OUTPUT = Path("data/pwc/")

df = pd.read_parquet(INPUT)

official = df["is_official"].eq(True)
paper = df["mentioned_in_paper"].eq(True)
github = df["mentioned_in_github"].eq(True)

# Preserve every original record.
df["link_status"] = "unverified"

df.loc[
    ~official & (paper | github),
    "link_status"
] = "candidate"

df.loc[
    official,
    "link_status"
] = "high_confidence"

# Save separate datasets.
df[df["link_status"] == "high_confidence"].to_parquet(
    OUTPUT / "high_confidence.parquet",
    index=False
)

df[df["link_status"] == "candidate"].to_parquet(
    OUTPUT / "candidate_links.parquet",
    index=False
)

df[df["link_status"] == "unverified"].to_parquet(
    OUTPUT / "unverified_links.parquet",
    index=False
)

print("\n=== GRAPHONE LINK CLASSIFICATION ===")
print(df["link_status"].value_counts())

print("\nTotal records:", len(df))
print("Classification complete.")
