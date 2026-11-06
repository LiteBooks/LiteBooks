"""Fail if a tool implemented in code is not offered by tools/list.

    python manage.py check_mcp_tools
        Checks this checkout: every @tool declared in ledger/mcp.py is registered
        once, serializes, and is offered to the highest role. Runs in CI.

    LITEBOOKS_MCP_TOKEN=lb_... python manage.py check_mcp_tools \\
        --url https://books.example.com/mcp/ --role viewer --expect-commit <sha>
        Checks a running server after a deploy: its tools/list must include every
        tool this checkout implements for the token's role, and server_info must
        report the expected commit.
"""

import ast
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.core.management.base import BaseCommand, CommandError

from ledger import mcp
from ledger.models import UserProfile
from ledger.permissions import ROLE_LEVEL


def declared_tool_names(source_path=None):
    """Every name passed to a @tool(...) decorator, in source order, duplicates included."""
    source_path = Path(source_path or mcp.__file__)
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", None) == "tool" and decorator.args:
                first = decorator.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.append(first.value)
    return names


def local_problems():
    declared = declared_tool_names()
    problems = []
    duplicates = sorted({name for name in declared if declared.count(name) > 1})
    if duplicates:
        problems.append(f"Declared more than once (the later one silently wins): {', '.join(duplicates)}")
    missing = sorted(set(declared) - set(mcp.TOOLS))
    if missing:
        problems.append(f"Declared with @tool but not registered: {', '.join(missing)}")
    top_level = max(ROLE_LEVEL.values())
    offered = {item.name for item in mcp.TOOLS.values() if top_level >= ROLE_LEVEL[item.minimum_role]}
    unreachable = sorted(set(declared) - offered)
    if unreachable:
        problems.append(f"Registered but offered to no role: {', '.join(unreachable)}")
    for item in mcp.TOOLS.values():
        try:
            json.dumps(item.definition())
        except (TypeError, ValueError) as error:
            problems.append(f"{item.name} cannot be serialized for tools/list: {error}")
    return declared, problems


def rpc(url, token, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    request = Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}", "User-Agent": "litebooks-check-mcp-tools"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as error:
        raise CommandError(f"{method} returned HTTP {error.code} from {url}.") from error
    except (URLError, OSError) as error:
        raise CommandError(f"Could not reach {url}: {error}") from error
    if "error" in payload:
        raise CommandError(f"{method} failed: {payload['error'].get('message')}")
    return payload["result"]


class Command(BaseCommand):
    help = "Fail if a tool implemented in ledger/mcp.py is missing from tools/list (locally, or on a running server with --url)."

    def add_arguments(self, parser):
        parser.add_argument("--url", help="MCP endpoint of a running server, e.g. https://books.example.com/mcp/. Token from LITEBOOKS_MCP_TOKEN.")
        parser.add_argument("--role", default=UserProfile.Role.VIEWER, choices=list(ROLE_LEVEL), help="Role of the token's user; only tools that role may use are expected. Default viewer.")
        parser.add_argument("--expect-commit", help="Fail unless server_info reports this git commit.")

    def handle(self, *args, **options):
        declared, problems = local_problems()
        if problems:
            raise CommandError("MCP tool registry problems:\n- " + "\n- ".join(problems))
        self.stdout.write(f"{len(declared)} tools declared and registered in this checkout.")
        if not options["url"]:
            return

        token = os.environ.get("LITEBOOKS_MCP_TOKEN", "").strip()
        if not token:
            raise CommandError("Set LITEBOOKS_MCP_TOKEN to an API token for the server.")
        level = ROLE_LEVEL[options["role"]]
        expected = {item.name for item in mcp.TOOLS.values() if level >= ROLE_LEVEL[item.minimum_role]}
        listed = {item["name"] for item in rpc(options["url"], token, "tools/list").get("tools", [])}
        missing = sorted(expected - listed)
        if missing:
            raise CommandError(f"tools/list at {options['url']} is missing {len(missing)} tool(s) this checkout implements for {options['role']}: {', '.join(missing)}")
        if options["expect_commit"]:
            if "server_info" not in listed:
                raise CommandError("The server does not offer server_info, so its commit cannot be checked.")
            result = rpc(options["url"], token, "tools/call", {"name": "server_info", "arguments": {}})
            commit = (result.get("structuredContent") or {}).get("git_commit")
            if commit != options["expect_commit"]:
                raise CommandError(f"server_info reports git_commit {commit!r}, expected {options['expect_commit']!r}.")
        self.stdout.write(self.style.SUCCESS(f"{options['url']} offers all {len(expected)} expected tools."))
