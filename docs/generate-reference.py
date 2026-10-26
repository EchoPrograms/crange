"""Generate references from syntax trees, never by importing automation modules."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def literal(node, bindings):
    """Evaluate only literal parser-loop values, never Python code or calls."""
    if isinstance(node, ast.Name) and node.id in bindings:
        return bindings[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal(node.left, bindings) + literal(node.right, bindings)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sorted" and len(node.args) == 1:
        return sorted(literal(node.args[0], bindings))
    return ast.literal_eval(node)


def parser_commands(nodes, bindings):
    commands = {}
    current = None
    for node in nodes:
        if isinstance(node, ast.For):
            for value in literal(node.iter, bindings):
                local = dict(bindings)
                if isinstance(node.target, ast.Name):
                    local[node.target.id] = value
                elif isinstance(node.target, ast.Tuple):
                    local.update((target.id, item) for target, item in zip(node.target.elts, value))
                commands.update(parser_commands(node.body, local))
            continue
        if isinstance(node, ast.If):
            branch = node.body if literal(node.test, bindings) else node.orelse
            # Conditional arguments belong to the already active parser.
            if current:
                for child in branch:
                    if isinstance(child, ast.Expr) and isinstance(child.value, ast.Call) and isinstance(child.value.func, ast.Attribute) and child.value.func.attr == "add_argument":
                        commands[current]["arguments"].append(argument(child.value))
            else:
                commands.update(parser_commands(branch, bindings))
            continue
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "add_parser":
                current = literal(call.args[0], bindings)
                help_node = next((k.value for k in call.keywords if k.arg == "help"), ast.Constant(""))
                try:
                    help_text = literal(help_node, bindings)
                except ValueError:
                    help_text = ""  # argparse.SUPPRESS is deliberately not evaluated.
                commands[current] = {"help": help_text, "arguments": []}
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and current:
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument":
                commands[current]["arguments"].append(argument(call))
    return commands


def argument(call):
    args = ", ".join(ast.unparse(a) for a in call.args)
    options = ", ".join(f"{k.arg}={ast.unparse(k.value)}" for k in call.keywords if k.arg != "type")
    return args + (", " + options if options else "")


def generate():
    target = ROOT / "docs/handbook/generated"
    target.mkdir(exist_ok=True)
    source = (ROOT / "scripts/crange.py").read_text()
    tree = ast.parse(source)
    parser = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "make_parser")
    lifecycle = ast.parse((ROOT / "scripts/crange_project_lifecycle.py").read_text())
    operation_node = next(n.value for n in lifecycle.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "OPERATIONS" for t in n.targets))
    commands = parser_commands(parser.body, {"OPERATIONS": ast.literal_eval(operation_node)})
    lines = ["# CLI command reference", "", "Generated from the checked-out parser declarations. No controller code is imported and no jobs run during this build.", "", "Run `scripts/crange COMMAND --help` on the installed controller for expanded choices, defaults and the authoritative option syntax. Expressions below are source declarations, not suggested shell arguments.", ""]
    for name, details in sorted(commands.items()):
        lines += [f"## {name}", "", details["help"], "", "```text", f"scripts/crange {name} --help", *details["arguments"], "```", ""]
    write_changed(target / "cli.md", "\n".join(lines))

    jobs = ast.parse((ROOT / "scripts/crange_jobs.py").read_text())
    function = next(n for n in jobs.body if isinstance(n, ast.FunctionDef) and n.name == "command")
    schemas = next(n.value for n in ast.walk(function) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "schemas" for t in n.targets))
    lines = ["# Queued operation reference", "", "Generated from the fixed `command()` allowlist in `scripts/crange_jobs.py`. These names are integration references; use the web forms for daily operations and secret input.", "", "The table lists validator keys, not a complete request specification. Validators, conditional requirements, defaults and actor checks remain authoritative in code. A queue operation can map to a differently named CLI command. Project lifecycle operations also have dedicated handling outside this table.", "", "| Operation | Validated option keys |", "| --- | --- |"]
    for key, value in sorted(zip(schemas.keys, schemas.values), key=lambda pair: ast.literal_eval(pair[0])):
        fields = value.elts[0]
        keys = [ast.literal_eval(k) for k in fields.keys]
        lines.append(f"| `{ast.literal_eval(key)}` | " + (", ".join(f"`{k}`" for k in keys) or "None") + " |")
    write_changed(target / "operations.md", "\n".join(lines) + "\n")

    server = ast.parse((ROOT / "scripts/crange_server.py").read_text())
    routes = set()
    for node in ast.walk(server):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/api/") and " " not in node.value and "\n" not in node.value:
            routes.add(node.value)
    lines = ["# Route inventory", "", "Literal API routes found in `scripts/crange_server.py`. Prefixes with a trailing slash have dynamic suffixes. This is a source inventory, not a promise that every method is available for every route or role. See the [API guide](../reference/api.md) for authentication and submission.", ""]
    lines += [f"- `{route}`" for route in sorted(routes)]
    write_changed(target / "api-routes.md", "\n".join(lines) + "\n")


def write_changed(path, content):
    if not path.exists() or path.read_text() != content:
        path.write_text(content)


if __name__ == "__main__":
    generate()
