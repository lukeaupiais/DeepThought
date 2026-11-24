"""Write the tsconfig.json that scip-typescript indexes a JavaScript corpus with.

scip-typescript reads tsconfig.json, not jsconfig.json. This copies the
repository's jsconfig.json (when there is one), turns on allowJs so every
JavaScript file is a source, turns off checkJs and drops ``types`` (no
dependencies are installed), and maps each ``--alias NAME=PATH`` with
``paths``, as the repository's own bundler or test runner aliases a
package's name to its source entry. Delete hand-written ``.d.ts`` files
first: TypeScript would bind a call to the declaration there instead of the
JavaScript function that runs.

Usage:
  python tools/javascript_tsconfig.py WORKSPACE [--alias NAME=PATH ...]
"""

import argparse
import json
from pathlib import Path


def tsconfig(workspace, aliases):
    workspace = Path(workspace)
    jsconfig = workspace / "jsconfig.json"
    config = json.loads(jsconfig.read_text("utf-8")) if jsconfig.is_file() else {}
    options = config.setdefault("compilerOptions", {})
    options.update({"allowJs": True, "checkJs": False, "noEmit": True})
    options.pop("types", None)
    if aliases:
        options["baseUrl"] = "."
        options["paths"] = {name: [path] for name, path in aliases}
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--alias", action="append", default=[], metavar="NAME=PATH")
    args = parser.parse_args(argv)
    aliases = [tuple(item.split("=", 1)) for item in args.alias]
    config = tsconfig(args.workspace, aliases)
    (args.workspace / "tsconfig.json").write_text(
        json.dumps(config, indent=2) + "\n", "utf-8"
    )


if __name__ == "__main__":
    main()
