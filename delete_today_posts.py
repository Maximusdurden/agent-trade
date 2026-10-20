import glob
import os

posts = glob.glob('Z:/python/projects/agent-trade/dashboard/**/*10-19*', recursive=True) + \
        glob.glob('Z:/python/projects/agent-trade/**/*10-19*.md', recursive=True) + \
        glob.glob('Z:/python/projects/agent-trade/**/*2026-10-19*', recursive=True)
print("Found posts/files for 10/19:", posts)
for p in posts:
    print("Deleting:", p)
    os.remove(p)
