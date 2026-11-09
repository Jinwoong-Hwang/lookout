"""Conditional focused review for interaction and failure pathways."""
import re


_CODE_FILE = re.compile(
    r"\.(?:[cm]?[jt]sx?|py|rb|go|rs|java|kt|swift|vue|svelte)$"
)
_TEST_FILE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|fixtures?|snapshots?)/|"
    r"(?:^|/)(?:test_[^/]+|[^/]+_test)\.py$|"
    r"_test\.go$|\.(?:test|spec)\.[^/]+$|\.snap$"
)
_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_PATHWAY_SIGNAL = re.compile(
    r"\b(?:onPress|onClick|onSubmit|onRetry|onClose|onFocus|onBlur|"
    r"onMount|onUnmount|useEffect|useLayoutEffect|retry|resend|refresh|reconnect|"
    r"onFailure|onFailed|onError|catch|reject|throw|except|finally|timeout|"
    r"navigate|router\.(?:push|replace)|addListener|removeListener|queue|flush|"
    r"enqueue|dequeue|worker|consume|\w*Callback|\w*Handler)\b",
    re.IGNORECASE,
)
_ACTION_SIGNAL = re.compile(
    r"\b(?:[Ss]end|[Ss]ubmit)(?:[A-Z]\w*|_\w+|\b)"
)
_HANDLER_ACTION_SIGNAL = re.compile(
    r"\bhandle_?(?:press|click|submit|send|retry|resend|refresh|reconnect|"
    r"failure|failed|error|close|focus|blur)\w*\b",
    re.IGNORECASE,
)


def _is_production_code_header(line: str) -> bool:
    match = _DIFF_HEADER.match(line)
    if not match:
        return False
    new_path = match.group(2)
    return bool(_CODE_FILE.search(new_path) and not _TEST_FILE.search(new_path))


def should_run(diff: str) -> bool:
    """Return whether added code changes an interaction/failure pathway."""
    in_code_file = False
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            in_code_file = _is_production_code_header(line)
            continue
        if not in_code_file or not line.startswith(("+", "-")):
            continue
        if line.startswith(("+++", "---")):
            continue
        changed = line[1:]
        if (_PATHWAY_SIGNAL.search(changed) or _ACTION_SIGNAL.search(changed)
                or _HANDLER_ACTION_SIGNAL.search(changed)):
            return True
    return False


def validate_result(result: dict) -> dict:
    """Fail closed when the focused pass did not prove which paths it checked."""
    coverage = result.get("coverage") if isinstance(result, dict) else None
    if not isinstance(coverage, list) or not coverage:
        raise ValueError("pathway audit coverage must be a non-empty list")
    required_text = ("outcome", "entry_point", "trigger", "success_sink", "failure_sink")
    valid_rows = all(
        isinstance(row, dict)
        and all(isinstance(row.get(field), str) and row[field].strip()
                for field in required_text)
        and isinstance(row.get("handler_chain"), list) and row["handler_chain"]
        and all(isinstance(step, str) and step.strip() for step in row["handler_chain"])
        and isinstance(row.get("evidence"), list) and row["evidence"]
        and all(isinstance(item, str) and item.strip() for item in row["evidence"])
        and isinstance(row.get("covered_by_change"), bool)
        for row in coverage
    )
    if not valid_rows:
        raise ValueError("pathway audit coverage rows are incomplete")
    if not isinstance(result.get("findings", []), list):
        raise ValueError("pathway audit findings must be a list")
    return result


def _finding_key(finding: dict) -> tuple[str, str]:
    identity = (finding.get("rule") or finding.get("title") or "").strip()
    return str(finding.get("file") or ""), identity


def merge_findings(primary: list[dict], audit: list[dict], limit: int) -> list[dict]:
    """Dedupe both passes and retain the highest-impact findings within the cap."""
    def impact(finding):
        severity = {"high": 0, "medium": 1}.get(finding.get("severity"), 2)
        confidence = {"high": 0, "medium": 1}.get(finding.get("confidence"), 2)
        return severity, confidence

    unique: dict[tuple[str, str], tuple[int, dict]] = {}
    all_findings: list[dict] = list(primary or []) + list(audit or [])
    for index, finding in enumerate(all_findings):
        key = _finding_key(finding)
        current = unique.get(key)
        if current is None:
            unique[key] = (index, finding)
        elif impact(finding) < impact(current[1]):
            unique[key] = (current[0], finding)

    ordered = [finding for _, finding in sorted(
        unique.values(), key=lambda item: (*impact(item[1]), item[0])
    )]
    return ordered[:max(0, int(limit))]
