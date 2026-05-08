from __future__ import annotations

import json
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


def build_source_context(source_root: str | Path | None) -> dict | None:
    if not source_root:
        return None

    root = Path(source_root).expanduser()
    if not root.exists():
        return {
            "version": SOURCE_CONTEXT_VERSION,
            "status": "error",
            "error": f"Source root does not exist: {source_root}",
        }
    if not root.is_dir():
        return {
            "version": SOURCE_CONTEXT_VERSION,
            "status": "error",
            "error": f"Source root is not a directory: {source_root}",
        }

    files = _walk_files(root)
    source_files = [path for path in files if path.suffix.lower() in SOURCE_EXTENSIONS][:MAX_SOURCE_FILES]
    package_info = _package_info(root)
    readmes = _readmes(root, files)
    routes = _routes(root, source_files)
    components = _components(root, source_files)
    ui_strings = _ui_strings(root, source_files)
    framework_hints = _framework_hints(package_info, files)

    return {
        "version": SOURCE_CONTEXT_VERSION,
        "status": "collected",
        "root": str(source_root),
        "root_name": root.resolve().name,
        "summary": {
            "file_count": len(files),
            "source_file_count": len(source_files),
            "tree_entry_count": min(len(files), MAX_TREE_ENTRIES),
            "route_count": len(routes),
            "component_count": len(components),
            "ui_string_count": len(ui_strings),
            "truncated": len(files) > MAX_TREE_ENTRIES or len(source_files) >= MAX_SOURCE_FILES,
        },
        "framework_hints": framework_hints,
        "package": package_info,
        "tree": [_rel(root, path) for path in files[:MAX_TREE_ENTRIES]],
        "readmes": readmes,
        "routes": routes,
        "components": components,
        "ui_strings": ui_strings,
    }


def _walk_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _is_skipped(root, path):
            continue
        files.append(path)
    return files


def _is_skipped(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True

    if any(part in SKIP_DIRS for part in relative.parts[:-1]):
        return True

    name = path.name
    if name in SKIP_FILES:
        return True
    if name.startswith(".env"):
        return True

    return False


def _package_info(root: Path) -> dict | None:
    package_path = root / "package.json"
    if package_path.exists():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
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
    if requirements_path.exists():
        requirements = []
        for line in _read_limited(requirements_path).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                requirements.append(line)
        return {
            "path": "requirements.txt",
            "dependencies": requirements[:80],
        }

    return None


def _readmes(root: Path, files: list[Path]) -> list[dict]:
    readme_files = [
        path
        for path in files
        if path.name.lower() in {"readme.md", "readme.txt", "readme"}
    ][:MAX_README_FILES]
    return [
        {
            "path": _rel(root, path),
            "text": _read_limited(path, MAX_README_CHARS),
        }
        for path in readme_files
    ]


def _routes(root: Path, source_files: list[Path]) -> list[dict]:
    routes = []
    seen = set()
    for path in source_files:
        for route in _file_based_routes(root, path):
            _append_unique(routes, seen, route, MAX_ROUTES)
        if len(routes) >= MAX_ROUTES:
            break

        if path.suffix.lower() not in FRONTEND_EXTENSIONS:
            continue
        text = _read_limited(path)
        for route in _regex_routes(root, path, text):
            _append_unique(routes, seen, route, MAX_ROUTES)
            if len(routes) >= MAX_ROUTES:
                break
        if len(routes) >= MAX_ROUTES:
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


def _components(root: Path, source_files: list[Path]) -> list[dict]:
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
        text = _read_limited(path)
        for pattern, kind in patterns:
            for match in re.finditer(pattern, text):
                item = {
                    "name": match.group(1),
                    "kind": kind,
                    "path": _rel(root, path),
                    "line": _line_number(text, match.start()),
                }
                _append_unique(components, seen, item, MAX_COMPONENTS)
                if len(components) >= MAX_COMPONENTS:
                    return components
    return components


def _ui_strings(root: Path, source_files: list[Path]) -> list[dict]:
    strings = []
    seen = set()
    for path in source_files:
        if path.suffix.lower() not in FRONTEND_EXTENSIONS:
            continue
        text = _read_limited(path)
        for value, position in _candidate_ui_strings(text):
            cleaned = _clean_ui_string(value)
            if not _is_useful_ui_string(cleaned):
                continue
            item = {
                "text": cleaned,
                "path": _rel(root, path),
                "line": _line_number(text, position),
            }
            _append_unique(strings, seen, item, MAX_UI_STRINGS)
            if len(strings) >= MAX_UI_STRINGS:
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


def _read_limited(path: Path, limit: int = MAX_FILE_CHARS) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
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
