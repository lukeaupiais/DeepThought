"""openapi-generator executions in Gradle builds.

Read from a ``build.gradle`` or ``build.gradle.kts`` that applies the
``org.openapi.generator`` plugin: the ``openApiGenerate { }`` block and
every task of type ``GenerateTask``. A setting is read when it is written
``name = value``, ``name.set(value)`` or ``name value`` at the top of the
block, and its value is a string literal (``.toString()`` allowed), a
boolean, or for ``configOptions`` a Groovy map literal or a Kotlin
``mapOf("k" to "v")``. ``$rootDir``, ``$projectDir`` and their
``${...}``/``project.``/``rootProject.`` forms are replaced; other
interpolation leaves the setting unknown. The plugin version is the one in
a ``plugins { id ... version ... }`` block (of the build or of the nearest
``settings.gradle``) or a buildscript ``openapi-generator-gradle-plugin``
classpath, with ``gradle.properties`` values replaced.
"""

from pathlib import PurePosixPath
import re

_BUILD_FILES = ("build.gradle", "build.gradle.kts")
_SETTINGS = ("settings.gradle", "settings.gradle.kts")
_PLUGIN_ID = "org.openapi.generator"
_SKIPPED = frozenset({"node_modules", "build", "target"})
_BLOCK_STARTS = re.compile(
    r"""(?mx)
    ^[ \t]*(?:
        openApiGenerate
      | task\s+\w+\s*\(\s*type\s*:\s*(?:[\w.]+\.)?GenerateTask\s*\)
      | tasks\.(?:register|create)\s*<\s*(?:[\w.]+\.)?GenerateTask\s*>\s*\(\s*["'][\w-]+["']\s*\)
      | tasks\.(?:register|create)\s*\(\s*["'][\w-]+["']\s*,\s*(?:[\w.]+\.)?GenerateTask
          (?:::class(?:\.java)?)?\s*\)
      | val\s+\w+\s+by\s+tasks\.(?:registering|creating)\s*\(\s*(?:[\w.]+\.)?GenerateTask
          ::class\s*\)
    )\s*\{"""
)
_UNKNOWN = object()


def _strip_comments(text):
    """The text with comments blanked (strings kept), offsets unchanged."""
    out, i, quote = [], 0, None
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if text.startswith(quote, i):
                out.append(text[i + 1 : i + len(quote)])
                i += len(quote)
                quote = None
                continue
            i += 1
            continue
        if text.startswith('"""', i) or text.startswith("'''", i):
            quote = text[i : i + 3]
            out.append(quote)
            i += 3
            continue
        if c in "\"'":
            quote = c
            out.append(c)
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = len(text) if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _block(text, start):
    """The text inside the braces opening at ``start``, at its depth only:
    nested braces are kept, so the caller sees top-level statements."""
    depth, i, quote = 0, start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1
    return None


def _statements(body):
    """(name, raw value) of the block's top-level settings."""
    depth, line, found = 0, [], []
    for c in body + "\n":
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if (c in "\n;") and depth <= 0:
            statement = "".join(line).strip()
            line = []
            match = re.match(
                r"^(\w+)\s*(?:=\s*(.+)|\.set\s*\((.+)\)|\s(.+))$", statement, re.S
            )
            if match:
                value = next(g for g in match.groups()[1:] if g is not None)
                found.append((match.group(1), value.strip()))
            continue
        line.append(c)
    return found


_DIRECTORIES = {
    "rootDir": "rootDir",
    "project.rootDir": "rootDir",
    "rootProject.rootDir": "rootDir",
    "rootProject.projectDir": "rootDir",
    "projectDir": "projectDir",
    "project.projectDir": "projectDir",
}


def _interpolate(text, dirs):
    """The string with directory references replaced; _UNKNOWN when it
    interpolates anything else."""
    unknown = []

    def replace(match):
        name = match.group(1) or match.group(2)
        if name not in _DIRECTORIES:
            unknown.append(name)
            return ""
        return dirs[_DIRECTORIES[name]]

    replaced = re.sub(r"\$\{\s*([\w.]+)\s*\}|\$(\w+)", replace, text)
    return _UNKNOWN if unknown else replaced


def _split(text):
    """Comma-separated parts outside quotes and brackets."""
    parts, current, depth, quote = [], [], 0, None
    for c in text:
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(c)
    parts.append("".join(current))
    return [p for p in parts if p.strip()]


def _string(raw, dirs):
    raw = re.sub(r"\.toString\(\)$", "", raw.strip())
    match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', raw)
    if match is None:
        if raw in ("true", "false"):
            return raw
        return _UNKNOWN
    if match.group(2) is not None:
        return match.group(2)  # single quotes do not interpolate
    return _interpolate(match.group(1), dirs)


def _map(raw, dirs):
    raw = raw.strip()
    if raw.startswith("mapOf(") and raw.endswith(")"):
        inner, pairs = raw[6:-1], r'"([\w.-]+)"\s+to\s+("(?:[^"\\]|\\.)*"|true|false)'
    elif raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        pairs = r"""["']?([\w.-]+)["']?\s*:\s*("(?:[^"\\]|\\.)*"|'[^']*'|true|false)"""
    else:
        return _UNKNOWN
    found = {}
    for entry in _split(inner):
        match = re.fullmatch(r"\s*" + pairs + r"\s*", entry, re.S)
        if match is None:
            return _UNKNOWN
        value = _string(match.group(2), dirs)
        if value is _UNKNOWN:
            return _UNKNOWN
        found[match.group(1)] = value
    return found


def _properties(root, directories):
    values = {}
    for directory in directories:
        path = root / directory / "gradle.properties"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw in text.splitlines():
            match = re.match(r"^\s*([\w.-]+)\s*[=:]\s*(.*?)\s*$", raw)
            if match and not raw.lstrip().startswith(("#", "!")):
                values[match.group(1)] = match.group(2)
    return values


def _version(texts, properties):
    for text in texts:
        match = re.search(
            r"""id\s*\(?\s*["']org\.openapi\.generator["']\s*\)?\s*version\s*\(?\s*["']([^"']+)["']""",
            text,
        ) or re.search(
            r"""["']org\.openapitools:openapi-generator-gradle-plugin:([^"']+)["']""",
            text,
        )
        if match:
            version = match.group(1)
            version = re.sub(
                r"\$\{?(\w+)\}?",
                lambda m: properties.get(m.group(1), m.group(0)),
                version,
            )
            return None if "$" in version else version
    return None


def gradle_executions(root):
    """(build file, line, version or None, configuration or reason) per
    openapi-generator execution in a Gradle build of the repository; the
    configuration has repository-relative paths and Maven option names."""
    found = []
    for path in sorted(p for name in _BUILD_FILES for p in root.rglob(name)):
        relative = path.relative_to(root)
        if any(p.startswith(".") or p in _SKIPPED for p in relative.parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _PLUGIN_ID not in text and "GenerateTask" not in text:
            continue
        code = _strip_comments(text)
        module = PurePosixPath(relative.as_posix()).parent
        settings_dir = next(
            (
                d
                for d in [module, *module.parents]
                if any((root / d / s).is_file() for s in _SETTINGS)
            ),
            module,
        )
        settings_texts = []
        for name in _SETTINGS:
            try:
                settings_texts.append(
                    _strip_comments((root / settings_dir / name).read_text("utf-8"))
                )
            except (OSError, UnicodeDecodeError):
                pass
        chain = [module, *module.parents]
        chain = chain[: chain.index(settings_dir) + 1]
        properties = _properties(root, reversed(chain))
        version = _version([code, *settings_texts], properties)
        dirs = {
            "rootDir": settings_dir.as_posix()
            if settings_dir != PurePosixPath(".")
            else "",
            "projectDir": module.as_posix() if module != PurePosixPath(".") else "",
        }
        # A leading "/" marks a repository-relative path below.
        dirs = {k: "\0" + v for k, v in dirs.items()}
        for start in _BLOCK_STARTS.finditer(code):
            body = _block(code, start.end() - 1)
            line = code.count("\n", 0, start.start()) + 1
            if body is None:
                continue
            config, options, reason = {}, {}, None
            for name, raw in _statements(body):
                if name == "configOptions":
                    value = _map(raw, dirs)
                    if value is _UNKNOWN:
                        reason = "config_not_literal"
                    else:
                        options = value
                    continue
                if name not in (
                    "generatorName",
                    "inputSpec",
                    "apiPackage",
                    "library",
                    "apiNamePrefix",
                    "apiNameSuffix",
                ):
                    continue
                value = _string(raw, dirs)
                if value is _UNKNOWN:
                    reason = "config_not_literal"
                    continue
                config[name] = value
            spec = config.get("inputSpec", "")
            if "\0" in spec:
                spec = spec[spec.rindex("\0") + 1 :]
            elif spec:
                # A relative path is relative to the project directory.
                spec = (module / spec).as_posix()
            config["inputSpec"] = spec
            config["configOptions"] = options
            found.append(
                (relative.as_posix(), line, version, reason if reason else config)
            )
    return found
