"""Static guarantees: the application source never calls send/create/delete.

Together with SafeGmailService (runtime allowlist, see test_gmail_client.py)
and the fake backend (which fails any send/create/delete call in every flow
test), this demonstrates that the application cannot send e-mails, create
drafts or delete drafts.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = sorted(p for p in ROOT.glob("*.py"))

FORBIDDEN_CALLS = {"send", "create", "delete", "trash", "batchDelete", "batchModify", "modify", "import_", "insert"}
GMAIL_RESOURCES = {"messages", "threads", "labels", "settings", "history"}


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def test_app_files_found():
    names = {p.name for p in APP_FILES}
    for required in ("main.py", "gui.py", "gmail_client.py", "file_scanner.py", "filename_parser.py",
                     "matching_engine.py", "database.py", "attachment_manager.py", "config.py"):
        assert required in names


def _is_gmail_receiver(node) -> bool:
    """True if the expression is (part of) a Gmail API resource chain."""
    text = ast.unparse(node)
    return any(marker in text for marker in ("users()", "drafts()", "_service", "service.", "gmail."))


def test_no_forbidden_gmail_methods_are_called():
    offenders = []
    for path in APP_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            receiver = call.func.value
            if call.func.attr in FORBIDDEN_CALLS and _is_gmail_receiver(receiver):
                offenders.append(f"{path.name}:{call.lineno} {ast.unparse(call.func)}()")
            if call.func.attr in GMAIL_RESOURCES and "users()" in ast.unparse(receiver):
                offenders.append(f"{path.name}:{call.lineno} {ast.unparse(call.func)}()")
    assert offenders == [], "Forbidden Gmail calls found: " + ", ".join(offenders)


def test_static_check_detects_a_send_call():
    """Self-check: the analysis above would catch a send call."""
    sample = ast.parse("service.users().drafts().send(userId='me', body={}).execute()")
    found = [c for c in _calls(sample) if c.func.attr == "send" and _is_gmail_receiver(c.func.value)]
    assert found


def test_only_allowed_draft_operations_are_used():
    """Every users().drafts().<method>() call is list, list_next, get or update."""
    allowed = {"list", "list_next", "get", "update"}
    used = set()
    for path in APP_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            target = call.func.value
            if isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute) and target.func.attr == "drafts":
                used.add(call.func.attr)
            elif isinstance(target, ast.Name) and target.id == "drafts":
                used.add(call.func.attr)
    assert used, "expected draft operations to be found"
    assert used <= allowed, f"unexpected draft operations: {used - allowed}"


def test_no_smtp_or_other_mail_sending():
    for path in APP_FILES:
        source = path.read_text(encoding="utf-8")
        assert "smtplib" not in source, path.name
        assert "send_message" not in source, path.name
        assert "sendmail" not in source, path.name


def test_requested_scope_is_minimal():
    from config import GMAIL_SCOPES
    assert GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.compose"]
