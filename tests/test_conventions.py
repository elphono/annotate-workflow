"""Repository conventions that a test can hold, so that review does not have to.

1. No deployment value in the code: no home directory, no distribution name,
   no IP but the loopback, no port outside `config.DEFAULT_PORT`, no Windows
   user path. Found at the AST, docstrings excluded, so that documentation
   may still QUOTE what it forbids (remarkable-sync's
   `test_aucune_valeur_de_deploiement_n_est_en_dur`, adapted).
2. Every module exposes its own exception.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sorted([*(ROOT / "src" / "annotate").glob("*.py"), *(ROOT / "hooks").glob("*.py"),
                 *(ROOT / "tools").glob("*.py")])
TEXT_ASSETS = sorted([*(ROOT / "src" / "annotate" / "static").glob("*"),
                      *(ROOT / "src" / "annotate" / "windows").glob("*.ps1")])

FORBIDDEN = (
    (r"/home/[A-Za-z0-9._-]+", "a home directory: use Path.home() or the config"),
    (r"(?i)\bubuntu|\bdebian\b", "a WSL distribution name: use WSL_DISTRO_NAME"),
    (r"(?i)c:\\+users", "a Windows user path: ask Windows (GetFolderPath)"),
    (r"(?<![\d.])(?!127\.0\.0\.1\b)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
     "an IP address other than the loopback"),
    (r"\b8765\b", "the default port: only config.DEFAULT_PORT may hold it"),
)


def _code_strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            head = body[0]
            if isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant) \
                    and isinstance(head.value.value, str):
                docs.add(id(head.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in docs:
            if isinstance(node.value, str):
                yield node.lineno, node.value
            elif isinstance(node.value, int) and not isinstance(node.value, bool):
                yield node.lineno, str(node.value)


def test_the_scan_covers_the_code():
    names = {p.name for p in PYTHON}
    assert {"server.py", "tray.py", "service.py", "register-on-write.py"} <= names
    assert {"annotate.js", "tray.ps1"} <= {p.name for p in TEXT_ASSETS}


def test_no_deployment_value_is_written_in_the_code():
    faults = []
    for path in PYTHON:
        for line, value in _code_strings(path):
            for pattern, why in FORBIDDEN:
                if re.search(pattern, value):
                    if path.name == "config.py" and value == "8765":
                        continue
                    faults.append(f"{path.name}:{line}: {why} -> {value[:60]!r}")
    for path in TEXT_ASSETS:
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern, why in FORBIDDEN:
                if re.search(pattern, text):
                    faults.append(f"{path.name}:{number}: {why} -> {text.strip()[:60]!r}")
    assert not faults, "\n".join(faults)


def test_the_detector_sees_what_it_forbids():
    # The witness: the same scan on a module that DOES contain each value.
    witness = 'A = "/home/someone/x"\nB = "Ubuntu-24.04"\nC = "10.0.0.2"\nD = 8765\n'
    sample = ROOT / "tests" / "_witness_module.py"
    sample.write_text(witness)
    try:
        found = [why for _, value in _code_strings(sample)
                 for pattern, why in FORBIDDEN if re.search(pattern, value)]
    finally:
        sample.unlink()
    assert len(found) == 4


def test_every_module_exposes_its_exception():
    missing = []
    for path in (ROOT / "src" / "annotate").glob("*.py"):
        if path.name in ("__init__.py", "cli.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)
                   and any(isinstance(b, ast.Name) and (b.id.endswith("Error") or b.id == "Exception")
                           for b in n.bases)]
        if not classes:
            missing.append(path.name)
    assert not missing, f"modules without their own exception: {missing}"
