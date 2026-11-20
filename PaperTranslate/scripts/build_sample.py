"""Prepare the public sample using only the authored, version-controlled note."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from papertranslate.exports import export


def main():
    sample = PROJECT / "papertranslate" / "sample"
    destination = PROJECT / "web"
    example = destination / "example"
    example.mkdir(exist_ok=True)
    document = json.loads((sample / "document.json").read_text())
    document.update(id="sample", status="ready")
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp) / "sample"
        shutil.copytree(sample, work)
        for kind, filename in [
            ("original", "original.pdf"), ("chinese", "chinese.pdf"),
            ("bilingual", "bilingual.pdf"), ("markdown", "paper.md")
        ]:
            shutil.copyfile(export(document, kind, work), example / filename)
    shutil.copyfile(example / "paper.md", example / "paper.md.txt")
    (destination / "sample.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
