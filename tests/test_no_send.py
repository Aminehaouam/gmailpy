"""Static guarantees about which Gmail operations the code can perform.

* Drafts are never created or deleted; messages.send is never used.
* drafts.send exists in exactly one place (GmailClient.send_draft) and is
  called from exactly one place (AttachmentManager._send, which runs only
  after a verified attachment and only when sending was requested).

Together with SafeGmailService (runtime allowlist, see test_gmail_client.py)
and the fake backend (which fails any create/delete call in every flow test).
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = sorted(p for p in ROOT.glob("*.py"))

FORBIDDEN_CALLS = {"create", "delete", "trash", "batchDelete", "batchModify", "modify", "import_", "insert"}
GMAIL_RESOURCES = {"messages", "threads", "labels", "settings", "history"}


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def _is_gmail_receiver(node) -> bool:
    text = ast.unparse(node)
    return any(marker in text for marker in ("users()", "drafts()", "_service", "service.", "gmail."))


def _enclosing_functions(tree):
    """Map each call node to the name of the function it is in."""
    owner = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                owner.setdefault(id(node), func.name)
    return owner


def test_app_files_found():
    names = {p.name for p in APP_FILES}
    for required in ("main.py", "gui.py", "gmail_client.py", "file_scanner.py", "filename_parser.py",
                     "matching_engine.py", "database.py", "attachment_manager.py", "config.py"):
        assert required in names


def test_no_create_delete_or_other_gmail_resources():
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


def test_sending_happens_in_exactly_one_place():
    gmail_sends, send_draft_callers = [], []
    for path in APP_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(tree)
        for call in _calls(tree):
            if call.func.attr == "send" and _is_gmail_receiver(call.func.value):
                gmail_sends.append((path.name, owner.get(id(call))))
            if call.func.attr == "send_draft":
                send_draft_callers.append((path.name, owner.get(id(call))))
    assert gmail_sends == [("gmail_client.py", "send_draft")]
    assert send_draft_callers == [("attachment_manager.py", "_send")]


def test_only_allowed_draft_operations_are_used():
    allowed = {"list", "list_next", "get", "update", "send"}
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


def test_no_smtp_or_messages_send():
    for path in APP_FILES:
        source = path.read_text(encoding="utf-8")
        assert "smtplib" not in source, path.name
        assert "sendmail" not in source, path.name


def test_static_check_detects_a_create_call():
    sample = ast.parse("service.users().drafts().create(userId='me', body={}).execute()")
    found = [c for c in _calls(sample) if c.func.attr == "create" and _is_gmail_receiver(c.func.value)]
    assert found


def test_requested_scope_is_minimal():
    from config import GMAIL_SCOPES
    assert GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.compose"]
