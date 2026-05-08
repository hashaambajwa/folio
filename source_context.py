from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from pathlib import Path


SOURCE_CONTEXT_VERSION = "0.1"
MAX_TREE_ENTRIES = 220
MAX_SOURCE_FILES = 260
MAX_README_FILES = 3
MAX_README_CHARS = 4_000
MAX_ROUTES = 80
MAX_COMPONENTS = 100
MAX_UI_STRINGS = 120
MAX_FILE_CHARS = 120_000
MAX_FILE_BYTES = 1_000_000

SKIP_DIRS = {
    ".cache",
    ".git",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".svelte-kit",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "outputs",
    "target",
    "vendor",
}
SKIP_FILES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
SKIP_EXTENSIONS = {
    ".avif",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".map",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}
SOURCE_EXTENSIONS = {
    ".astro",
    ".html",
    ".js",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
}
FRONTEND_EXTENSIONS = {".astro", ".html", ".js", ".jsx", ".mjs", ".svelte", ".ts", ".tsx", ".vue"}


@dataclass(frozen=True)
class SourceContextLimits:
    max_tree_entries: int = MAX_TREE_ENTRIES
    max_source_files: int = MAX_SOURCE_FILES
    max_readme_files: int = MAX_README_FILES
    max_readme_chars: int = MAX_README_CHARS
    max_routes: int = MAX_ROUTES
    max_components: int = MAX_COMPONENTS
    max_ui_strings: int = MAX_UI_STRINGS
    max_file_chars: int = MAX_FILE_CHARS
    max_file_bytes: int = MAX_FILE_BYTES


@dataclass
class SourceScanStats:
    skipped_symlinks: int = 0
    skipped_outside_root: int = 0
    skipped_large_files: int = 0
    skipped_unreadable_files: int = 0
    skipped_policy_files: int = 0


def build_source_context(
    source_root: str | Path | None,
    max_tree_entries: int = MAX_TREE_ENTRIES,
    max_source_files: int = MAX_SOURCE_FILES,
    max_readme_files: int = MAX_README_FILES,
    max_readme_chars: int = MAX_README_CHARS,
    max_routes: int = MAX_ROUTES,
    max_components: int = MAX_COMPONENTS,
    max_ui_strings: int = MAX_UI_STRINGS,
    max_file_chars: int = MAX_FILE_CHARS,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> dict | None:
    if not source_root:
        return None

    limits = SourceContextLimits(
        max_tree_entries=max(0, int(max_tree_entries)),
        max_source_files=max(0, int(max_source_files)),
        max_readme_files=max(0, int(max_readme_files)),
        max_readme_chars=max(0, int(max_readme_chars)),
        max_routes=max(0, int(max_routes)),
        max_components=max(0, int(max_components)),
        max_ui_strings=max(0, int(max_ui_strings)),
        max_file_chars=max(0, int(max_file_chars)),
        max_file_bytes=max(0, int(max_file_bytes)),
    )

    requested_root = Path(source_root).expanduser()
    if not requested_root.exists():
        return {
            "version": SOURCE_CONTEXT_VERSION,
            "status": "error",
            "error": f"Source root does not exist: {source_root}",
        }

    try:
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        return {
            "version": SOURCE_CONTEXT_VERSION,
            "status": "error",
            "error": f"Source root could not be resolved: {type(exc).__name__}: {exc}",
        }

    if not root.is_dir():
        return {
            "version": SOURCE_CONTEXT_VERSION,
            "status": "error",
            "error": f"Source root is not a directory: {source_root}",
        }

    stats = SourceScanStats()
    files = _walk_files(root, limits, stats)
    all_source_files = [path for path in files if path.suffix.lower() in SOURCE_EXTENSIONS]
    source_files = _select_source_files(root, all_source_files, limits.max_source_files)
    package_info = _package_info(root, limits, stats)
    readme_files = _readme_files(files)
    readmes = _readmes(root, readme_files, limits, stats)
    routes = _routes(root, source_files, limits, stats)
    components = _components(root, source_files, limits, stats)
    ui_strings = _ui_strings(root, source_files, limits, stats)
    framework_hints = _framework_hints(package_info, files)
    tree = _tree_entries(root, files, source_files, limits.max_tree_entries)
    diagnostics = _diagnostics(
        files=files,
        all_source_files=all_source_files,
        source_files=source_files,
        tree=tree,
        readmes=readmes,
        readme_file_count=len(readme_files),
        routes=routes,
        components=components,
        ui_strings=ui_strings,
        limits=limits,
        stats=stats,
    )

    return {
        "version": SOURCE_CONTEXT_VERSION,
        "status": "collected",
        "root": str(source_root),
        "root_name": root.resolve().name,
        "limits": {
            "max_tree_entries": limits.max_tree_entries,
            "max_source_files": limits.max_source_files,
            "max_readme_files": limits.max_readme_files,
            "max_readme_chars": limits.max_readme_chars,
            "max_routes": limits.max_routes,
            "max_components": limits.max_components,
            "max_ui_strings": limits.max_ui_strings,
            "max_file_chars": limits.max_file_chars,
            "max_file_bytes": limits.max_file_bytes,
        },
        "summary": {
            "file_count": len(files),
            "source_file_count": len(all_source_files),
            "source_files_inspected": len(source_files),
            "tree_entry_count": len(tree),
            "route_count": len(routes),
            "component_count": len(components),
            "ui_string_count": len(ui_strings),
            "truncated": diagnostics["truncated"],
        },
        "diagnostics": diagnostics,
        "framework_hints": framework_hints,
        "package": package_info,
        "tree": tree,
        "inspected_files": [_rel(root, path) for path in source_files],
        "readmes": readmes,
        "routes": routes,
        "components": components,
        "ui_strings": ui_strings,
    }


def _walk_files(root: Path, limits: SourceContextLimits, stats: SourceScanStats) -> list[Path]:
    files = []
    for current_dir, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current_dir)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not _skip_directory(root, current_path / dirname, stats)
        ]

        for filename in sorted(filenames):
            path = current_path / filename
            if _is_skipped(root, path, limits, stats):
                continue
            files.append(path)

    return files


def _skip_directory(root: Path, path: Path, stats: SourceScanStats) -> bool:
    if path.is_symlink():
        stats.skipped_symlinks += 1
        return True

    if path.name in SKIP_DIRS:
        stats.skipped_policy_files += 1
        return True

    try:
        resolved_path = path.resolve(strict=True)
    except OSError:
        stats.skipped_unreadable_files += 1
        return True

    if not _is_relative_to(resolved_path, root):
        stats.skipped_outside_root += 1
        return True

    return False


def _is_skipped(root: Path, path: Path, limits: SourceContextLimits, stats: SourceScanStats) -> bool:
    if path.is_symlink():
        stats.skipped_symlinks += 1
        return True

    try:
        resolved_path = path.resolve(strict=True)
    except OSError:
        stats.skipped_unreadable_files += 1
        return True

    if not _is_relative_to(resolved_path, root):
        stats.skipped_outside_root += 1
        return True

    if not path.is_file():
        stats.skipped_policy_files += 1
        return True

    relative = resolved_path.relative_to(root)
    if any(part in SKIP_DIRS for part in relative.parts[:-1]):
        stats.skipped_policy_files += 1
        return True

    name = path.name
    if name in SKIP_FILES:
        stats.skipped_policy_files += 1
        return True
    if name.startswith(".env"):
        stats.skipped_policy_files += 1
        return True
    if path.suffix.lower() in SKIP_EXTENSIONS:
        stats.skipped_policy_files += 1
        return True

    if _file_too_large(path, limits):
        stats.skipped_large_files += 1
        return True

    return False


def _safe_file_exists(root: Path, path: Path, limits: SourceContextLimits, stats: SourceScanStats) -> bool:
    if not path.exists() and not path.is_symlink():
        return False

    if _is_skipped(root, path, limits, stats):
        return False
    return path.is_file()


def _file_too_large(path: Path, limits: SourceContextLimits) -> bool:
    try:
        return path.stat().st_size > limits.max_file_bytes
    except OSError:
        return True


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _select_source_files(root: Path, source_files: list[Path], limit: int) -> list[Path]:
    return sorted(source_files, key=lambda path: (_source_file_priority(root, path), _rel(root, path)))[:limit]


def _source_file_priority(root: Path, path: Path) -> tuple[int, int]:
    relative = Path(_rel(root, path))
    parts = relative.parts
    name = path.name.lower()
    suffix = path.suffix.lower()
    depth = len(parts)

    if name in {"package.json", "requirements.txt", "pyproject.toml"}:
        return (0, depth)
    if name.startswith("readme"):
        return (1, depth)
    if _is_route_file(parts, name):
        return (2, depth)
    if name in {"app.jsx", "app.tsx", "app.js", "app.ts", "main.jsx", "main.tsx", "main.js", "main.ts"}:
        return (3, depth)
    if "components" in parts or suffix in {".jsx", ".tsx", ".svelte", ".vue", ".astro"}:
        return (4, depth)
    if "src" in parts:
        return (5, depth)
    return (8, depth)


def _is_route_file(parts: tuple[str, ...], name: str) -> bool:
    if any(part in {"pages", "routes"} for part in parts):
        return True
    if "app" in parts and name in {"page.js", "page.jsx", "page.ts", "page.tsx"}:
        return True
    if name.startswith("+page"):
        return True
    return False


def _tree_entries(root: Path, files: list[Path], source_files: list[Path], limit: int) -> list[str]:
    selected: list[str] = []
    seen = set()
    if limit <= 0:
        return selected

    shallow_limit = max(1, limit // 2)

    for path in sorted(files, key=lambda path: (len(Path(_rel(root, path)).parts), _rel(root, path))):
        _append_path(selected, seen, _rel(root, path), shallow_limit)
        if len(selected) >= shallow_limit:
            break

    for path in source_files:
        _append_path(selected, seen, _rel(root, path), limit)
        if len(selected) >= limit:
            return selected

    for path in sorted(files, key=lambda path: (len(Path(_rel(root, path)).parts), _rel(root, path))):
        _append_path(selected, seen, _rel(root, path), limit)
        if len(selected) >= limit:
            return selected

    return selected


def _append_path(items: list[str], seen: set[str], path: str, limit: int) -> None:
    if path in seen or len(items) >= limit:
        return
    seen.add(path)
    items.append(path)


def _package_info(root: Path, limits: SourceContextLimits, stats: SourceScanStats) -> dict | None:
    package_path = root / "package.json"
    if _safe_file_exists(root, package_path, limits, stats):
        try:
            package = json.loads(_read_limited(root, package_path, limits, stats))
        except (OSError, json.JSONDecodeError):
            package = {}

        dependencies = {
            **package.get("dependencies", {}),
            **package.get("devDependencies", {}),
        }
        return {
            "path": "package.json",
            "name": package.get("name"),
            "version": package.get("version"),
            "scripts": package.get("scripts", {}),
            "dependencies": sorted(dependencies)[:80],
        }

    requirements_path = root / "requirements.txt"
    if _safe_file_exists(root, requirements_path, limits, stats):
        requirements = []
        for line in _read_limited(root, requirements_path, limits, stats).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                requirements.append(line)
        return {
            "path": "requirements.txt",
            "dependencies": requirements[:80],
        }

    return None


def _readme_files(files: list[Path]) -> list[Path]:
    return [
        path
        for path in files
        if path.name.lower() in {"readme.md", "readme.txt", "readme"}
    ]


def _readmes(
    root: Path,
    readme_files: list[Path],
    limits: SourceContextLimits,
    stats: SourceScanStats,
) -> list[dict]:
    return [
        {
            "path": _rel(root, path),
            "text": _read_limited(root, path, limits, stats, limits.max_readme_chars),
        }
        for path in readme_files[: limits.max_readme_files]
    ]


def _routes(root: Path, source_files: list[Path], limits: SourceContextLimits, stats: SourceScanStats) -> list[dict]:
    routes = []
    seen = set()
    for path in source_files:
        for route in _file_based_routes(root, path):
            _append_unique(routes, seen, route, limits.max_routes)
        if len(routes) >= limits.max_routes:
            break

        if path.suffix.lower() not in FRONTEND_EXTENSIONS:
            continue
        text = _read_limited(root, path, limits, stats)
        for route in _regex_routes(root, path, text):
            _append_unique(routes, seen, route, limits.max_routes)
            if len(routes) >= limits.max_routes:
                break
        if len(routes) >= limits.max_routes:
            break

    return routes


def _file_based_routes(root: Path, path: Path) -> list[dict]:
    relative = Path(_rel(root, path))
    parts = relative.parts
    routes = []

    if "pages" in parts:
        index = parts.index("pages")
        route_parts = list(parts[index + 1 :])
        if route_parts and route_parts[0] == "api":
            return []
        route = _route_from_file_parts(route_parts)
        if route:
            routes.append({"route": route, "source": "file_route", "path": str(relative)})

    if "app" in parts and path.name in {"page.js", "page.jsx", "page.ts", "page.tsx"}:
        index = parts.index("app")
        route = _route_from_file_parts(list(parts[index + 1 : -1]))
        routes.append({"route": route or "/", "source": "file_route", "path": str(relative)})

    if "routes" in parts:
        index = parts.index("routes")
        route = _route_from_file_parts(list(parts[index + 1 :]))
        if route:
            routes.append({"route": route, "source": "file_route", "path": str(relative)})

    if "src" in parts and "routes" in parts and path.name.startswith("+page"):
        index = parts.index("routes")
        route = _route_from_file_parts(list(parts[index + 1 : -1]))
        routes.append({"route": route or "/", "source": "file_route", "path": str(relative)})

    return routes


def _route_from_file_parts(parts: list[str]) -> str | None:
    cleaned = []
    for part in parts:
        stem = Path(part).stem
        if stem in {"index", "page", "+page", "_app", "_document"}:
            continue
        if stem.startswith("(") and stem.endswith(")"):
            continue
        segment = stem.replace("$", ":")
        segment = re.sub(r"\[+(\w+)\]+", r":\1", segment)
        segment = segment.replace(".", "/")
        if segment:
            cleaned.extend(value for value in segment.split("/") if value)

    if not cleaned:
        return "/"
    return "/" + "/".join(cleaned)


def _regex_routes(root: Path, path: Path, text: str) -> list[dict]:
    route_patterns = [
        (r"<Route[^>]+path=[\"'`]([^\"'`]+)[\"'`]", "react_route"),
        (r"\bpath\s*:\s*[\"'`](/[^\"'`]+)[\"'`]", "route_config"),
        (r"\bhref=[\"'`](/[^\"'`#][^\"'`]*)[\"'`]", "internal_link"),
    ]
    routes = []
    for pattern, source in route_patterns:
        for match in re.finditer(pattern, text):
            route = match.group(1).strip()
            if route and not route.startswith("//"):
                routes.append(
                    {
                        "route": route,
                        "source": source,
                        "path": _rel(root, path),
                        "line": _line_number(text, match.start()),
                    }
                )
    return routes


def _components(
    root: Path,
    source_files: list[Path],
    limits: SourceContextLimits,
    stats: SourceScanStats,
) -> list[dict]:
    patterns = [
        (r"\bexport\s+default\s+function\s+([A-Z][A-Za-z0-9_]*)", "default_function"),
        (r"\bexport\s+function\s+([A-Z][A-Za-z0-9_]*)", "function"),
        (r"\bfunction\s+([A-Z][A-Za-z0-9_]*)\s*\(", "function"),
        (r"\bconst\s+([A-Z][A-Za-z0-9_]*)\s*=", "const"),
        (r"\bclass\s+([A-Z][A-Za-z0-9_]*)\s+", "class"),
    ]
    components = []
    seen = set()
    for path in source_files:
        if path.suffix.lower() not in {".astro", ".js", ".jsx", ".mjs", ".svelte", ".ts", ".tsx", ".vue"}:
            continue
        text = _read_limited(root, path, limits, stats)
        for pattern, kind in patterns:
            for match in re.finditer(pattern, text):
                item = {
                    "name": match.group(1),
                    "kind": kind,
                    "path": _rel(root, path),
                    "line": _line_number(text, match.start()),
                }
                _append_unique(components, seen, item, limits.max_components)
                if len(components) >= limits.max_components:
                    return components
    return components


def _ui_strings(
    root: Path,
    source_files: list[Path],
    limits: SourceContextLimits,
    stats: SourceScanStats,
) -> list[dict]:
    strings = []
    seen = set()
    for path in source_files:
        if path.suffix.lower() not in FRONTEND_EXTENSIONS:
            continue
        text = _read_limited(root, path, limits, stats)
        for value, position in _candidate_ui_strings(text):
            cleaned = _clean_ui_string(value)
            if not _is_useful_ui_string(cleaned):
                continue
            item = {
                "text": cleaned,
                "path": _rel(root, path),
                "line": _line_number(text, position),
            }
            _append_unique(strings, seen, item, limits.max_ui_strings)
            if len(strings) >= limits.max_ui_strings:
                return strings
    return strings


def _candidate_ui_strings(text: str) -> list[tuple[str, int]]:
    candidates = []
    for match in re.finditer(r"[\"'`]([^\"'`\\n]{3,100})[\"'`]", text):
        candidates.append((match.group(1), match.start()))
    for match in re.finditer(r">([^<>{}\\n]{3,100})<", text):
        candidates.append((match.group(1), match.start()))
    return candidates


def _clean_ui_string(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _is_useful_ui_string(value: str) -> bool:
    if len(value) < 3 or len(value) > 100:
        return False
    if not re.search(r"[A-Za-z]", value):
        return False
    if re.search(r"\.(js|jsx|ts|tsx|css|png|jpg|svg|json)$", value):
        return False
    if value.startswith(("/", "./", "../", "http:", "https:", "#")):
        return False
    if re.fullmatch(r"[a-z0-9_-]+", value):
        return False
    if any(token in value for token in ("${", "=>", "://", "node_modules")):
        return False
    return True


def _framework_hints(package_info: dict | None, files: list[Path]) -> list[str]:
    hints = set()
    dependencies = set(package_info.get("dependencies", []) if package_info else [])
    dependency_hints = {
        "@angular/core": "Angular",
        "@remix-run/react": "Remix",
        "astro": "Astro",
        "django": "Django",
        "fastapi": "FastAPI",
        "flask": "Flask",
        "next": "Next.js",
        "react": "React",
        "svelte": "Svelte",
        "vue": "Vue",
        "vite": "Vite",
    }
    for dependency, hint in dependency_hints.items():
        if dependency in dependencies:
            hints.add(hint)

    filenames = {path.name for path in files}
    if "next.config.js" in filenames or "next.config.mjs" in filenames:
        hints.add("Next.js")
    if "vite.config.ts" in filenames or "vite.config.js" in filenames:
        hints.add("Vite")
    if "svelte.config.js" in filenames:
        hints.add("Svelte")

    return sorted(hints)


def _diagnostics(
    files: list[Path],
    all_source_files: list[Path],
    source_files: list[Path],
    tree: list[str],
    readmes: list[dict],
    readme_file_count: int,
    routes: list[dict],
    components: list[dict],
    ui_strings: list[dict],
    limits: SourceContextLimits,
    stats: SourceScanStats,
) -> dict:
    flags = {
        "tree_truncated": len(files) > len(tree),
        "source_files_truncated": len(all_source_files) > len(source_files),
        "readmes_truncated": readme_file_count > len(readmes),
        "routes_truncated": len(routes) >= limits.max_routes,
        "components_truncated": len(components) >= limits.max_components,
        "ui_strings_truncated": len(ui_strings) >= limits.max_ui_strings,
    }
    return {
        "files_seen": len(files),
        "source_files_seen": len(all_source_files),
        "source_files_inspected": len(source_files),
        "skipped_symlinks": stats.skipped_symlinks,
        "skipped_outside_root": stats.skipped_outside_root,
        "skipped_large_files": stats.skipped_large_files,
        "skipped_unreadable_files": stats.skipped_unreadable_files,
        "skipped_policy_files": stats.skipped_policy_files,
        **flags,
        "truncated": any(flags.values()),
    }


def _read_limited(
    root: Path,
    path: Path,
    limits: SourceContextLimits,
    stats: SourceScanStats,
    limit: int | None = None,
) -> str:
    if not _safe_file_exists(root, path, limits, stats):
        return ""

    read_limit = limit if limit is not None else limits.max_file_chars
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as file:
            return file.read(read_limit)
    except OSError:
        stats.skipped_unreadable_files += 1
        return ""


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _append_unique(items: list[dict], seen: set[tuple], item: dict, limit: int) -> None:
    key = tuple(sorted((name, str(value)) for name, value in item.items()))
    if key in seen or len(items) >= limit:
        return
    seen.add(key)
    items.append(item)


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
