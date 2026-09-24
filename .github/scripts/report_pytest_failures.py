"""Turns a pytest JUnit XML report into GitHub Actions `::error::` workflow
commands. Step logs on GitHub need a signed-in browser, but annotations are
readable through the public API -- so a failed run's reason stays
diagnosable by anyone (or any tool) without a login. GitHub shows at most
~10 error annotations per step, so only the first failures are emitted; the
full count is always included in the summary line."""

import sys
import xml.etree.ElementTree as ET

MAX_ANNOTATIONS = 10
MAX_MESSAGE_CHARS = 900


def _escape(text: str) -> str:
    # Workflow-command data escaping (see GitHub's toolkit `command.ts`).
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    # Property values (title=...) additionally must not contain `:` or `,`.
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def main(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::error title=pytest report unreadable::{_escape(str(exc))}")
        return 0

    failures = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        detail = (problem.get("message") or "") + "\n" + (problem.text or "")
        failures.append((name, detail.strip()))

    print(f"pytest reported {len(failures)} failing test(s)")
    for name, detail in failures[:MAX_ANNOTATIONS]:
        message = _escape(detail[-MAX_MESSAGE_CHARS:])
        print(f"::error title={_escape_property(name)}::{message}")
    if len(failures) > MAX_ANNOTATIONS:
        print(f"::error title=more failures::{len(failures) - MAX_ANNOTATIONS} more not shown")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "report.xml"))
