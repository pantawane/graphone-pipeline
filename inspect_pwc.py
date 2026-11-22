import pandas as pd

file_path = "data/pwc/train-00000-of-00001.parquet"

df = pd.read_parquet(file_path)

print("\n=== ALL DATASET COLUMNS ===")
print(df.columns.tolist())

print("\n=== DATASET SIZE ===")
print(f"Total records: {len(df)}")

print("\n=== LINK QUALITY DISTRIBUTION ===")

for col in [
    "is_official",
    "mentioned_in_paper",
    "mentioned_in_github"
]:
    print(f"\n{col}:")
    print(df[col].value_counts(dropna=False))

    print("\n=== FLAG COMBINATIONS ===")

flags = [
    "is_official",
    "mentioned_in_paper",
    "mentioned_in_github"
]

combinations = (
    df.groupby(flags, dropna=False)
    .size()
    .reset_index(name="count")
    .sort_values("count", ascending=False)
)

print(combinations.to_string(index=False))

# Compare filtering strategies
official = df["is_official"].eq(True)
paper_mentioned = df["mentioned_in_paper"].eq(True)
github_mentioned = df["mentioned_in_github"].eq(True)

print("\n=== FILTERING STRATEGIES ===")

print("Official only:", official.sum())

print("Official OR mentioned in paper:",
      (official | paper_mentioned).sum())

print("Official AND mentioned in paper:",
      (official & paper_mentioned).sum())

print("At least one of all three:",
      (official | paper_mentioned | github_mentioned).sum())

print("None of the three:",
      (~(official | paper_mentioned | github_mentioned)).sum())