"""Spring server interfaces that openapi-generator makes from an OpenAPI spec.

A spec-first Spring service keeps its routes in an OpenAPI document and has
``openapi-generator-maven-plugin`` turn it into Java interfaces at build
time; its controllers implement them, so no mapping annotation is in the
repository. Read here, for an execution of that plugin in a ``pom.xml``:

- generator ``spring`` (library ``spring-boot`` or none), plugin version 7
  or later, ``interfaceOnly`` true, and no ``delegatePattern``: the
  interfaces then carry each operation's method and path and no base path
  (``requestMappingMode`` ``controller``, the default, or ``none``);
- ``inputSpec`` a file in the repository (``${project.basedir}``,
  ``${basedir}`` and the pom's own properties are replaced);
- each operation's interface is ``<apiNamePrefix><Group><apiNameSuffix>``
  in ``apiPackage``, where the group is the operation's first tag with
  ``useTags`` and the first path segment without it, and its Java method is
  the ``operationId`` with a lower-case first letter.

Anything else (another generator or library, an older plugin, a generated
controller, a base path on the interfaces, an operationId that is not a
plain identifier) is counted as unread, not guessed.
"""

from dataclasses import dataclass
import re
import xml.etree.ElementTree as ElementTree

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_JAVA_KEYWORDS = frozenset(
    """abstract assert boolean break byte case catch char class const continue
    default do double else enum extends final finally float for goto if
    implements import instanceof int interface long native new package private
    protected public return short static strictfp super switch synchronized
    this throw throws transient try void volatile while true false null
    var record yield""".split()
)
_PLUGIN = "openapi-generator-maven-plugin"


@dataclass(frozen=True)
class SpecOperation:
    """One operation of a spec, as the Java method of a generated interface."""

    interface: str  # qualified Java name of the generated interface
    method_name: str
    http_method: str  # upper case
    path: str
    spec_file: str
    spec_line: int


@dataclass(frozen=True)
class OpenApiUnread:
    file: str
    line: int
    reason: str


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(element, name):
    return next((c for c in element if _local(c.tag) == name), None)


def _child_text(element, name):
    found = None if element is None else _child(element, name)
    return found.text.strip() if found is not None and found.text else None


def _camelize(name):
    """openapi-generator's camelize of a sanitized name: every run of letters
    and digits with its first letter in upper case."""
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", name))


def _line_of(text, needle):
    index = text.find(needle)
    return 1 if index < 0 else text.count("\n", 0, index) + 1


def _operation_line(text, operation_id):
    """The line of the operationId, in YAML or JSON; 1 if not found."""
    if not isinstance(operation_id, str):
        return 1
    match = re.search(
        r"""["']?operationId["']?\s*:\s*["']?"""
        + re.escape(operation_id)
        + r"""(?![\w$-])""",
        text,
        re.MULTILINE,
    )
    return 1 if match is None else text.count("\n", 0, match.start()) + 1


def _major(version):
    match = re.match(r"(\d+)\.", version or "")
    return int(match.group(1)) if match else None


def _substitute(value, properties):
    def replace(match):
        return properties.get(match.group(1), match.group(0))

    for _ in range(5):
        replaced = re.sub(r"\$\{([^}]+)\}", replace, value)
        if replaced == value:
            break
        value = replaced
    return value


def _executions(project):
    """(plugin version, merged configuration dict) per plugin execution."""
    build = _child(project, "build")
    plugins = []
    management = None if build is None else _child(build, "pluginManagement")
    for container in (build, management):
        if container is None:
            continue
        found = _child(container, "plugins")
        if found is not None:
            plugins.extend(p for p in found if _local(p.tag) == "plugin")
    for plugin in plugins:
        if _child_text(plugin, "artifactId") != _PLUGIN:
            continue
        version = _child_text(plugin, "version")
        shared = _configuration(_child(plugin, "configuration"))
        executions = _child(plugin, "executions")
        for execution in [] if executions is None else executions:
            if _local(execution.tag) != "execution":
                continue
            merged = {**shared}
            own = _configuration(_child(execution, "configuration"))
            options = {
                **shared.get("configOptions", {}),
                **own.get("configOptions", {}),
            }
            merged.update(own)
            merged["configOptions"] = options
            yield version, merged


def _configuration(element):
    found = {}
    if element is None:
        return found
    for child in element:
        name = _local(child.tag)
        if name == "configOptions":
            found[name] = {
                _local(o.tag): (o.text or "").strip()
                for o in child
                if isinstance(o.tag, str)
            }
        elif isinstance(child.tag, str) and len(child) == 0:
            found[name] = (child.text or "").strip()
    return found


def generated_spring_apis(root):
    """Operations of the Spring interfaces generated in the repository, by
    qualified interface name, and the unread executions and operations."""
    try:
        import yaml
    except ImportError:  # pyyaml comes with the languages extra
        return {}, []
    apis, unread = {}, []
    for pom in sorted(root.rglob("pom.xml")):
        relative = pom.relative_to(root)
        if any(
            p.startswith(".") or p in ("node_modules", "target") for p in relative.parts
        ):
            continue
        try:
            project = ElementTree.parse(pom).getroot()
        except (ElementTree.ParseError, OSError):
            continue
        pom_file = relative.as_posix()
        pom_text = pom.read_text(encoding="utf-8", errors="replace")
        line = _line_of(pom_text, _PLUGIN)
        module = relative.parent
        properties = {
            "project.basedir": "",
            "basedir": "",
        }
        found = _child(project, "properties")
        for element in [] if found is None else found:
            if isinstance(element.tag, str) and element.text:
                properties[_local(element.tag)] = element.text.strip()
        for version, config in _executions(project):
            options = config["configOptions"]

            def flag(name, options=options, config=config):
                value = options.get(name, config.get(name, ""))
                return _substitute(value, properties).lower() == "true"

            generator = config.get("generatorName", "")
            library = options.get("library", config.get("library", ""))
            mode = options.get("requestMappingMode", "controller")
            if generator != "spring" or library not in ("", "spring-boot"):
                reason = "not_spring_server"
            elif (_major(_substitute(version or "", properties)) or 0) < 7:
                reason = "generator_version"
            elif not flag("interfaceOnly") or flag("delegatePattern"):
                reason = "generated_controller"
            elif mode not in ("controller", "none") or flag(
                "useRequestMappingOnInterface"
            ):
                reason = "base_path_on_interface"
            else:
                reason = None
            if reason is not None:
                unread.append(OpenApiUnread(pom_file, line, reason))
                continue
            spec = _substitute(config.get("inputSpec", ""), properties).lstrip("/")
            package = _substitute(config.get("apiPackage", ""), properties)
            path = root / module / spec
            if not spec or "${" in spec or not path.is_file() or not package:
                unread.append(OpenApiUnread(pom_file, line, "spec_not_found"))
                continue
            spec_file = path.resolve().relative_to(root.resolve()).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError:
                document = None
            if not isinstance(document, dict) or not isinstance(
                document.get("paths"), dict
            ):
                unread.append(OpenApiUnread(spec_file, 1, "spec_not_parsed"))
                continue
            prefix = _substitute(config.get("apiNamePrefix", ""), properties)
            suffix = _substitute(config.get("apiNameSuffix", "Api"), properties)
            use_tags = flag("useTags")
            for route, item in document["paths"].items():
                if not isinstance(item, dict):
                    continue
                for http_method in HTTP_METHODS:
                    operation = item.get(http_method)
                    if not isinstance(operation, dict):
                        continue
                    operation_id = operation.get("operationId")
                    op_line = _operation_line(text, operation_id)
                    if not isinstance(operation_id, str) or not re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9]*", operation_id
                    ):
                        unread.append(OpenApiUnread(spec_file, op_line, "operation_id"))
                        continue
                    name = operation_id[0].lower() + operation_id[1:]
                    if name in _JAVA_KEYWORDS:
                        unread.append(OpenApiUnread(spec_file, op_line, "operation_id"))
                        continue
                    if use_tags:
                        tags = operation.get("tags") or ["default"]
                        group = str(tags[0])
                    else:
                        group = route.lstrip("/").split("/", 1)[0] or "default"
                    camel = _camelize(group)
                    if "{" in group or not camel or not camel[0].isalpha():
                        unread.append(OpenApiUnread(spec_file, op_line, "api_name"))
                        continue
                    interface = f"{package}.{prefix}{camel}{suffix}"
                    apis.setdefault(interface, []).append(
                        SpecOperation(
                            interface,
                            name,
                            http_method.upper(),
                            route,
                            spec_file,
                            op_line,
                        )
                    )
    return apis, unread
