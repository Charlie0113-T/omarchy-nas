"""Build the self-contained zipapp into dist/; run from the source directory."""
from pathlib import Path
import zipfile

root = Path(__file__).parent
target = root / "dist" / "omarchy-nas.pyz"
target.parent.mkdir(exist_ok=True)
# Hidden paths are .git and tool caches, never part of the program.
skip = {"dist", "__pycache__"}
with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as out:
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        hidden = any(part.startswith(".") for part in relative.parts)
        if file.is_file() and not hidden and not skip.intersection(relative.parts) and file.suffix != ".pyc":
            out.write(file, relative)
print(target)
