"""Planning and compact context building for documentation PR reviews."""
import fnmatch
import json
import os
from pathlib import Path


TEXT_EXTS = {
    ".md", ".mdx", ".txt", ".csv", ".tsv", ".yaml", ".yml", ".json",
    ".feature", ".html",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
BINARY_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".zip"}

ARTIFACT_PATTERNS = {
    "prd": [
        "docs/prd/*.md", "docs/PRD*.md", "_prd*.md", "2-Pager*.md",
        "docs/2-Pager*.md",
    ],
    "architecture": [
        "artifacts/architecture/*.md", "spec/architecture.md",
        "docs/architecture.md",
    ],
    "spec": [
        "artifacts/spec/*.md", "spec/*.md", "docs/ui-spec.md",
        "docs/integration-review.md",
    ],
    "test": [
        "artifacts/test-case/*.md", "AC/*", "docs/AC*.md",
        "artifacts/acceptance-criteria/*", "docs/acceptance-criteria*",
        "*.feature", "artifacts/features/*.feature",
    ],
    "screen": [
        "docs/Screen/**", "screenshots/**", "gallery.html", "REPORT.md",
        "docs/Screen/REPORT.md",
    ],
    "ref": ["ref/**", "docs/references/**"],
}


def _ext(path: str) -> str:
    return Path(path).suffix.lower()


def _is_text(path: str) -> bool:
    return _ext(path) in TEXT_EXTS


def _epic_root(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "epics":
        return "/".join(parts[:2])
    return None


def _rel_to_root(path: str, root: str) -> str:
    prefix = root.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else path


def _match_any(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def artifact_type(path: str) -> str:
    root = _epic_root(path)
    rel = _rel_to_root(path, root) if root else path
    if path.endswith("audit-log.jsonl"):
        return "audit"
    if _ext(path) in IMAGE_EXTS:
        return "image"
    if _ext(path) in BINARY_EXTS:
        return "binary"
    for kind, patterns in ARTIFACT_PATTERNS.items():
        if _match_any(rel, patterns):
            return kind
    return "other"


def _structure_for(root: str, files: list[str]) -> str:
    root_files = [p for p in files if p.startswith(root.rstrip("/") + "/")]
    zax = any("/artifacts/" in p or "/docs/prd/" in p for p in root_files)
    legacy = any("/spec/" in p or "/docs/PRD" in p or "/AC/" in p for p in root_files)
    if zax and legacy:
        return "mixed"
    if zax:
        return "zax"
    if legacy:
        return "legacy"
    return "unknown"


def _shape(types: set[str], roots: list[str], summary_only: bool) -> str:
    if summary_only:
        return "large_summary_only"
    doc_types = types - {"audit", "image", "binary", "ref", "other"}
    if types & {"screen", "image"} and len(doc_types) <= 1:
        return "screen_report_heavy"
    if doc_types == {"prd"}:
        return "prd_only"
    if doc_types and doc_types <= {"spec", "test"}:
        return "spec_test_only"
    if len(doc_types) >= 2:
        return "multi_artifact"
    if "audit" in types and len(types) <= 2:
        return "generated_heavy"
    if len(roots) > 1:
        return "drift_fix"
    return "unknown"


def _review_mode(shape: str, types: set[str]) -> str:
    if shape == "large_summary_only":
        return "summary_only"
    if shape == "prd_only":
        return "prd_quality"
    if shape == "multi_artifact" or shape == "drift_fix":
        return "artifact_consistency"
    if types & {"test"}:
        return "testability"
    if types & {"spec", "architecture"}:
        return "implementation_readiness"
    return "artifact_consistency"


def _read_file(path: str, cap: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(cap + 1)
    except OSError:
        return ""
    if len(text) > cap:
        return text[:cap] + "\n…(truncated)"
    return text


def _safe_path(wt: str, rel: str) -> str | None:
    root = Path(wt).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return str(target)


def _candidate_artifacts(wt: str, root: str) -> list[str]:
    base = os.path.join(wt, root)
    if not os.path.isdir(base):
        return []
    out = []
    for dirpath, _, names in os.walk(base):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), wt)
            if artifact_type(rel) in {"prd", "architecture", "spec", "test", "screen"} and _is_text(rel):
                out.append(rel)
    return sorted(out)


def build_plan(repo: str, pr: int, wt: str, diff: str, changed_files: list[str], policy: dict) -> dict:
    roots = sorted({r for r in (_epic_root(p) for p in changed_files) if r})
    types = {artifact_type(p) for p in changed_files}
    large_cfg = policy.get("large_pr") or {}
    diff_lines = len(diff.splitlines())
    summary_only = (
        len(changed_files) > int(large_cfg.get("files", 20))
        or diff_lines > int(large_cfg.get("diff_lines", 3000))
        or len(roots) > int(large_cfg.get("epic_roots", 2))
    )
    shape = _shape(types, roots, summary_only)
    structures = {_structure_for(root, changed_files) for root in roots} or {"unknown"}
    structure = structures.pop() if len(structures) == 1 else "mixed"
    plan = {
        "repo": repo,
        "pr": pr,
        "review_mode": _review_mode(shape, types),
        "shape": shape,
        "epic_roots": roots,
        "structure": structure,
        "artifact_types": sorted(types),
        "summary_only": summary_only,
        "binary_present": bool(types & {"binary"}),
        "image_present": bool(types & {"image"}),
        "diff_lines": diff_lines,
        "changed_file_count": len(changed_files),
        "context_budget": {"diff_chars": 120000, "file_chars": 60000},
        "changed_files": changed_files,
    }
    return plan


def build_context(wt: str, diff: str, changed_files: list[str], plan: dict) -> str:
    file_budget = int((plan.get("context_budget") or {}).get("file_chars", 60000))
    per_file = 12000
    chunks = [
        "## Changed files",
        "\n".join(f"- {p} [{artifact_type(p)}]" for p in changed_files) or "(none)",
        "",
        "## Diff",
        diff[: int((plan.get("context_budget") or {}).get("diff_chars", 120000))],
    ]
    if plan.get("summary_only"):
        chunks.extend([
            "",
            "## Summary-only note",
            "This PR exceeds the configured review size threshold. Do not produce findings; summarize why human review is needed.",
        ])
        return "\n".join(chunks)
    used = 0
    seen = set()

    def add_file(rel: str, reason: str):
        nonlocal used
        if rel in seen or not _is_text(rel) or used >= file_budget:
            return
        path = _safe_path(wt, rel)
        if not path:
            return
        text = _read_file(path, min(per_file, file_budget - used))
        if not text:
            return
        seen.add(rel)
        used += len(text)
        chunks.extend(["", f"## {reason}: {rel}", text])

    for rel in changed_files:
        add_file(rel, "Changed file")

    for root in plan.get("epic_roots") or []:
        for rel in _candidate_artifacts(wt, root):
            add_file(rel, "Same-epic artifact")

    if plan.get("image_present"):
        chunks.extend([
            "",
            "## Image handling note",
            "Image/screenshot files are present, but their visual contents are not interpreted here.",
        ])

    return "\n".join(chunks)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
