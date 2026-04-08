"""Detect public functions with zero call sites — prevents dead code accumulation."""
import ast
import subprocess
from pathlib import Path

REPO = Path.home() / "code" / "mtor"
SOURCE_DIRS = [REPO / "mtor"]

# Entrypoints called by frameworks, not by our code
ALLOWLIST = {
    "main",           # worker entrypoint
    "translate",      # Temporal activity
    "merge_approved", # Temporal activity  
    "watch_cycle",    # Temporal activity
    "chaperone",      # Temporal activity
}


def _find_public_functions() -> list[tuple[str, str]]:
    """Return (filepath, func_name) for all public functions."""
    results = []
    for src_dir in SOURCE_DIRS:
        for pyfile in src_dir.rglob("*.py"):
            try:
                tree = ast.parse(pyfile.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_") and node.name not in ALLOWLIST:
                        results.append((str(pyfile.relative_to(REPO)), node.name))
    return results


def _count_calls(func_name: str) -> int:
    """Count non-definition references to func_name in the codebase."""
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "-w", func_name, str(REPO / "mtor")],
        capture_output=True, text=True,
    )
    count = 0
    for line in result.stdout.splitlines():
        stripped = line.split(":", 1)[1].strip() if ":" in line else line
        # Skip definitions and comments
        if stripped.startswith("def ") or stripped.startswith("async def ") or stripped.startswith("#"):
            continue
        count += 1
    return count


def test_no_unwired_public_functions():
    dead = []
    for filepath, func_name in _find_public_functions():
        if _count_calls(func_name) == 0:
            dead.append(f"{filepath}:{func_name}")
    assert not dead, f"Dead public functions (defined but never called):\n" + "\n".join(f"  - {d}" for d in dead)
