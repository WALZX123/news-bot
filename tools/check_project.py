"""Offline syntax, template and public-file credential checks (no app startup)."""
from __future__ import annotations

import ast
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".venv", ".idea", "__pycache__", "backups", ".backups", "runtime",
            "private-backup", "node_modules", "dist", "local-tools", ".cache", ".npm-cache", ".pytest_cache"}
TEXT_TYPES = {".py", ".yaml", ".yml", ".json", ".md", ".txt", ".toml", ".html",
              ".js", ".ts", ".tsx", ".css", ".ps1", ".cmd", ".lock"}
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}"),
    re.compile(r"access_token=[A-Za-z0-9_-]{24,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
]


def source_files():
    for base, folders, files in os.walk(ROOT):
        folders[:] = [name for name in folders if name.casefold() not in EXCLUDED]
        yield from (Path(base) / name for name in files)


def private_path(path):
    name = path.name.casefold()
    return (bool(EXCLUDED & {part.casefold() for part in path.parts[:-1]}) or
            name.startswith(("config.yaml", "config.yml", "config.local.")) or
            (name.startswith(".env") and name != ".env.example") or
            any(name.endswith(suffix) for suffix in (".bak", ".dump", ".pfx", ".pem", ".key", ".dpapi")) or
            re.search(r"\.(?:log|sqlite(?:3)?|db)(?:$|[.-])", name) is not None)


def public_files(staged):
    if staged or (ROOT / ".git").exists():
        # Include accidentally tracked private paths so they can be rejected.
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
        return [Path(name) for name in result.stdout.decode("utf-8").split("\0") if name]
    return [path.relative_to(ROOT) for path in source_files() if not private_path(path.relative_to(ROOT))]


def read_text(path, staged):
    if staged:
        result = subprocess.run(["git", "show", ":" + path.as_posix()], cwd=ROOT, check=True, capture_output=True)
        return result.stdout.decode("utf-8-sig")
    return (ROOT / path).read_text(encoding="utf-8-sig")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--staged", action="store_true", help="Read actual Git index bytes for source and credential checks.")
    staged = cli.parse_args().staged
    paths = public_files(staged)
    errors = []
    python_files = [path for path in paths if path.suffix == ".py"]
    for path in python_files:
        try:
            ast.parse(read_text(path, staged), filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            errors.append(f"syntax: {path} ({type(exc).__name__})")
    env = Environment(loader=FileSystemLoader(str(ROOT / "web/templates")))
    # Discover factory-registered filter names without creating the application
    # (which would connect to MySQL). Only compilation happens in this tool.
    tree=ast.parse((ROOT/'web/__init__.py').read_text(encoding='utf-8-sig'))
    for node in ast.walk(tree):
        if (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                and node.func.attr=='template_filter' and node.args
                and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str)):
            env.filters[node.args[0].value]=lambda value,*args,**kwargs:value
    names = env.list_templates()
    for name in names:
        try:
            env.get_template(name)
        except Exception as exc:
            errors.append(f"template: {name} ({type(exc).__name__})")
    for path in paths:
        if private_path(path):
            errors.append(f"private path tracked by Git: {path}")
            continue
        if path.suffix.lower() not in TEXT_TYPES:
            continue
        try:
            text = read_text(path, staged)
        except (OSError, UnicodeError):
            errors.append(f"unreadable source: {path}")
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            # Never print the line or matched secret.
            errors.append(f"possible credential in public file: {path}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Offline checks passed: {len(python_files)} Python files, {len(names)} templates; no known credential patterns in public files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
