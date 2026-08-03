"""Scans a Python file for invalid escape sequences inside string literals.
Finds backslashes followed by characters that are not valid Python escapes
and are NOT part of an intentional double-backslash."""
import re
import sys

path = sys.argv[1]
valid_escapes = set("nrtbfva\\'\"01234567xuN")

with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines, start=1):
    # Find backslashes not part of a double backslash
    for m in re.finditer(r"(?<!\\)\\(?!\\)", line):
        idx = m.start()
        if idx + 1 < len(line):
            nxt = line[idx + 1]
            if nxt not in valid_escapes:
                # Print context around the offending sequence
                start = max(0, idx - 40)
                end = min(len(line), idx + 2 + 40)
                snippet = line[start:end].rstrip("\n")
                print(f"L{i}: \\{nxt}  ...{snippet}...")
