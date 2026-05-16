import difflib
from pathlib import Path

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.webm', '.mov', '.m4v', '.ts'}


class LocalLibrary:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def scan(self) -> list[Path]:
        return [
            p for p in self.root.rglob('*')
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ]

    def search(self, query: str, n: int = 5) -> list[Path]:
        files = self.scan()
        if not files:
            return []

        stems = [p.stem for p in files]
        stem_map: dict[str, Path] = {p.stem: p for p in files}

        close = difflib.get_close_matches(query, stems, n=n, cutoff=0.3)
        seen: set[str] = set(close)
        results = [stem_map[m] for m in close if m in stem_map]

        # Add substring matches not caught by difflib
        q = query.lower()
        for p in files:
            if p.stem not in seen and q in p.stem.lower():
                results.append(p)
                seen.add(p.stem)
                if len(results) >= n:
                    break

        return results[:n]
