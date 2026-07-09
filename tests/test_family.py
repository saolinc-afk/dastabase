from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent

sys.path.append(str(PROJECT_ROOT))

from enrichers.family import detect_family

with open(
    "cache/websites/3.html",
    encoding="utf-8"
) as f:

    html = f.read()

result = detect_family(html)

print()
print("=" * 60)
print("Family Detection Test")
print("=" * 60)
print()

print(result)

print()