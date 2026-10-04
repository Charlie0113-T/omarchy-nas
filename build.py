"""Build the self-contained zipapp; run from the extracted source directory."""
from pathlib import Path
import zipfile

root = Path(__file__).parent
target = root.parent / "omarchy-nas.pyz"
with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as out:
    for file in sorted(root.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            out.write(file, file.relative_to(root))
print(target)
