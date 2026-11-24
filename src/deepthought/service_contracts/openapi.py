"""Spring server interfaces and Java client classes that openapi-generator
makes from an OpenAPI spec.

A spec-first Spring service keeps its routes in an OpenAPI document and has
``openapi-generator-maven-plugin`` turn it into Java interfaces at build
time; its controllers implement them, so no mapping annotation is in the
repository. A client module has the same plugin generate a Java API class
per tag, whose methods send the operations. Read here, for an execution of
that plugin in a ``pom.xml`` (with the version and configuration its parent
poms in the repository give the plugin), or of the Gradle plugin
``org.openapi.generator`` (``openapi_gradle``):

- generator ``spring`` (library ``spring-boot`` or none), plugin version 7
  or later, ``interfaceOnly`` true, and no ``delegatePattern``: the
  interfaces then carry each operation's method and path and no base path
  (``requestMappingMode`` ``controller``, the default, or ``none``);
- generator ``spring`` with library ``spring-cloud``, plugin version 7 or
  later, ``interfaceOnly`` false: ``<Api>Client`` in ``apiPackage`` is a
  Feign client of the interface's operations (its ``@FeignClient`` URL is a
  property or service discovery, so its base is not fixed);
- generator ``java``, plugin version 4 or later, with a library whose API
  classes send each operation from a method named after it (the default
  ``okhttp-gson``, ``native``, ``resttemplate``, ``webclient``,
  ``restclient``, ``apache-httpclient``, ``jersey2``, ``jersey3``,
  ``feign``); the base URL is the spec's first server, as the generator
  reads it;
- ``inputSpec`` a file in the repository (``${project.basedir}``,
  ``${basedir}`` and the poms' properties are replaced);
- each operation is in ``<apiNamePrefix><Group><apiNameSuffix>`` (Spring
  takes no prefix) in ``apiPackage``, once per group: its tags for a java
  client, and for Spring its tags with ``useTags`` and its first path
  segment without it; its Java
  method is the ``operationId`` with a lower-case first letter.

Anything else (another generator or library, an older plugin, a generated
controller, a base path on the interfaces, an operationId that is not a
plain identifier) is counted as unread, not guessed.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ElementTree

from .http import PLACEHOLDER
from .openapi_gradle import gradle_executions

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
LOCAL_HOST = "http://localhost"
# Java client libraries whose generated API classes have one method per
# operation, named from its operationId, that sends the request.
_JAVA_LIBRARIES = frozenset(
    """okhttp-gson native resttemplate webclient restclient apache-httpclient
    jersey2 jersey3 feign""".split()
) | {""}
# The generators' apiPackage when the configuration sets none.
_DEFAULT_PACKAGES = {
    "java": "org.openapitools.client.api",
    "spring": "org.openapitools.api",
}
# Generated methods that send an operation, by suffix after its name.
CLIENT_METHOD_SUFFIXES = ("", "WithHttpInfo", "Async", "WithHttpInfoAsync")


@dataclass(frozen=True)
class SpecOperation:
    """One operation of a spec, as the Java method of a generated interface."""

    interface: str  # qualified Java name of the generated interface
    method_name: str
    http_method: str  # upper case
    path: str
    spec_file: str
    spec_line: int
    # A client's base URL, from the spec's servers; "" for a server.
    base_url: str = ""
    # The generated client: "openapi-java" (a java API class) or
    # "openapi-spring-cloud" (a Feign client); "" for a server.
    client: str = ""


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


def _plugins(project):
    """The openapi-generator plugin elements of a pom's build and its
    pluginManagement, in that order."""
    build = _child(project, "build")
    management = None if build is None else _child(build, "pluginManagement")
    for container in (build, management):
        found = None if container is None else _child(container, "plugins")
        for plugin in [] if found is None else found:
            if _local(plugin.tag) == "plugin":
                if _child_text(plugin, "artifactId") == _PLUGIN:
                    yield plugin


def _merge(base, own):
    merged = {**base, **own}
    merged["configOptions"] = {
        **base.get("configOptions", {}),
        **own.get("configOptions", {}),
    }
    return merged


def _executions(project, inherited=None):
    """(plugin version, merged configuration dict) per plugin execution.

    ``inherited`` is the (version, configuration) the parent poms give the
    plugin; the pom's own plugin configuration and then the execution's
    override it, configOptions entry by entry."""
    version, shared = inherited or (None, {"configOptions": {}})
    plugins = list(_plugins(project))
    for plugin in plugins:
        version = _child_text(plugin, "version") or version
        shared = _merge(shared, _configuration(_child(plugin, "configuration")))
    for plugin in plugins:
        executions = _child(plugin, "executions")
        for execution in [] if executions is None else executions:
            if _local(execution.tag) != "execution":
                continue
            own = _configuration(_child(execution, "configuration"))
            yield version, _merge(shared, own)


def _inherited(project):
    """(version, configuration) of the plugin in this pom, without executions:
    what its child modules inherit."""
    version, shared = None, {"configOptions": {}}
    for plugin in _plugins(project):
        version = _child_text(plugin, "version") or version
        shared = _merge(shared, _configuration(_child(plugin, "configuration")))
    return version, shared


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


def _server_url(document):
    """The base URL openapi-generator gives a client: the first server's URL
    with its variables' defaults, ``http://localhost`` before a relative URL
    and when the spec names none (Swagger 2: scheme, host and basePath)."""
    if "swagger" in document:
        host = document.get("host")
        base = document.get("basePath") or ""
        schemes = document.get("schemes") or ["http"]
        url = f"{schemes[0]}://{host}{base}" if isinstance(host, str) else base
    else:
        servers = document.get("servers")
        server = servers[0] if isinstance(servers, list) and servers else {}
        url = server.get("url", "") if isinstance(server, dict) else ""
        variables = server.get("variables") if isinstance(server, dict) else None
        if isinstance(url, str) and isinstance(variables, dict):

            def default(match):
                variable = variables.get(match.group(1))
                if isinstance(variable, dict) and "default" in variable:
                    return str(variable["default"])
                return match.group(0)

            url = re.sub(r"\{([^}]+)\}", default, url)
    if not isinstance(url, str) or not url:
        return LOCAL_HOST
    if url.startswith("//"):
        url = "http:" + url
    elif url.startswith("/"):
        url = LOCAL_HOST + url
    if not re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", url):
        return LOCAL_HOST  # not a URL: the generator falls back too
    return url.rstrip("/")


def _settings_file(path, yaml):
    """The plain settings of a configurationFile (JSON or YAML), as text."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    return {
        str(k): str(v).lower() if isinstance(v, bool) else str(v)
        for k, v in document.items()
        if isinstance(v, (str, bool, int)) and k != "configOptions"
    }


def _parent(poms, relative, project):
    """The parent pom in the repository, if its relativePath (default
    ``../pom.xml``) names a pom whose artifactId is the declared parent's."""
    parent = _child(project, "parent")
    if parent is None:
        return None
    found = _child(parent, "relativePath")
    if found is not None and not (found.text or "").strip():
        return None  # <relativePath/>: looked up in repositories only
    written = "../pom.xml" if found is None else found.text.strip()
    target = PurePosixPath(relative.parent.as_posix(), written)
    if not written.endswith(".xml"):
        target = target / "pom.xml"
    parts = []
    for part in target.parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    candidate = "/".join(parts)
    if candidate not in poms:
        return None
    if _child_text(poms[candidate][0], "artifactId") != _child_text(
        parent, "artifactId"
    ):
        return None
    return candidate


def _spec_operations(document, text, spec_file, group_names, unread):
    """(group, method name, HTTP method, path, line) per operation; an
    operation whose method or group name is not read is counted unread."""
    for route, item in document["paths"].items():
        if not isinstance(item, dict) or not isinstance(route, str):
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
            groups = group_names(route, operation)
            if any(
                "{" in g or not _camelize(g) or not _camelize(g)[0].isalpha()
                for g in groups
            ):
                unread.append(OpenApiUnread(spec_file, op_line, "api_name"))
                continue
            for group in groups:
                yield group, name, http_method.upper(), route, op_line


def _tags(route, operation):
    """Every tag of the operation, ``default`` without one."""
    tags = operation.get("tags")
    if not isinstance(tags, list) or not tags:
        return ["default"]
    return list(dict.fromkeys(str(t) for t in tags))


def _first_segment(route, operation):
    return [route.lstrip("/").split("/", 1)[0] or "default"]


def _generate(
    root,
    yaml,
    pom_file,
    line,
    module,
    version,
    config,
    properties,
    servers,
    clients,
    unread,
):
    """Read one plugin execution (Maven or Gradle) of ``pom_file``."""
    written = _substitute(config.get("configurationFile", ""), properties)
    if written:
        # Its settings apply under the pom's own.
        found = _settings_file(root / module / written.lstrip("/"), yaml)
        if found is None:
            unread.append(OpenApiUnread(pom_file, line, "config_not_read"))
            return
        config = {**found, **config}
    options = config["configOptions"]

    def flag(name, options=options, config=config):
        value = options.get(name, config.get(name, ""))
        return _substitute(value, properties).lower() == "true"

    generator = _substitute(config.get("generatorName", ""), properties)
    library = _substitute(options.get("library", config.get("library", "")), properties)
    major = _major(_substitute(version or "", properties)) or 0
    mode = options.get("requestMappingMode", "controller")
    cloud = generator == "spring" and library == "spring-cloud"
    client = generator == "java" or cloud
    if cloud:
        # The Feign clients are generated only with the interfaces' default
        # implementation (SpringCodegen: not interfaceOnly).
        if major < 7:
            reason = "generator_version"
        elif flag("interfaceOnly"):
            reason = "client_not_generated"
        else:
            reason = None
    elif client:
        if library not in _JAVA_LIBRARIES:
            reason = "client_library"
        elif major < 4:
            reason = "generator_version"
        else:
            reason = None
    elif generator != "spring" or library not in ("", "spring-boot"):
        reason = "not_spring_server"
    elif major < 7:
        reason = "generator_version"
    elif not flag("interfaceOnly") or flag("delegatePattern"):
        reason = "generated_controller"
    elif mode not in ("controller", "none") or flag("useRequestMappingOnInterface"):
        reason = "base_path_on_interface"
    else:
        reason = None
    if reason is not None:
        unread.append(OpenApiUnread(pom_file, line, reason))
        return
    spec = _substitute(config.get("inputSpec", ""), properties).lstrip("/")
    package = _substitute(
        config.get("apiPackage", _DEFAULT_PACKAGES[generator]), properties
    )
    path = root / module / spec
    if not spec or "${" in spec or not path.is_file() or not package:
        unread.append(OpenApiUnread(pom_file, line, "spec_not_found"))
        return
    spec_file = path.resolve().relative_to(root.resolve()).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        document = None
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        unread.append(OpenApiUnread(spec_file, 1, "spec_not_parsed"))
        return
    prefix = _substitute(config.get("apiNamePrefix", ""), properties)
    suffix = _substitute(config.get("apiNameSuffix", "Api"), properties)
    if generator == "java":
        group_names, base, target = _tags, _server_url(document), clients
        kind = "openapi-java"
    else:
        group_names = _tags if flag("useTags") else _first_segment
        # A Feign client's URL is the ${<api var>.url:<base>} property, or
        # service discovery without useFeignClientUrl: not fixed either way.
        base, target = (PLACEHOLDER, clients) if cloud else ("", servers)
        kind = "openapi-spring-cloud" if cloud else ""
    for group, name, http_method, route, op_line in _spec_operations(
        document, text, spec_file, group_names, unread
    ):
        if generator == "java":
            # toApiName: camelize(prefix + "_" + tag + "_" + suffix).
            api = _camelize(f"{prefix}_{_camelize(group)}_{suffix}")
        else:
            # SpringCodegen.toApiName: camelize(tag) + suffix, no prefix.
            api = _camelize(group) + suffix
        interface = f"{package}.{api}" + ("Client" if cloud else "")
        target.setdefault(interface, []).append(
            SpecOperation(
                interface,
                name,
                http_method,
                route,
                spec_file,
                op_line,
                base,
                kind,
            )
        )


def generated_apis(root):
    """What openapi-generator executions in the repository's poms generate:
    Spring server interfaces and Java client classes, each by qualified
    name with its operations, and the unread executions and operations.

    A module inherits the plugin's version and configuration from its
    parent poms in the repository (``<parent><relativePath>``). Gradle
    builds are read by ``openapi_gradle``."""
    try:
        import yaml
    except ImportError:  # pyyaml comes with the languages extra
        return {}, {}, []
    servers, clients, unread = {}, {}, []
    poms = {}
    for pom in sorted(root.rglob("pom.xml")):
        relative = pom.relative_to(root)
        if any(
            p.startswith(".") or p in ("node_modules", "target") for p in relative.parts
        ):
            continue
        try:
            poms[relative.as_posix()] = (ElementTree.parse(pom).getroot(), pom)
        except (ElementTree.ParseError, OSError):
            continue

    def lineage(pom_file):
        """The pom and its parents in the repository, nearest first."""
        chain, current = [], pom_file
        while current is not None and current not in chain and len(chain) < 20:
            chain.append(current)
            current = _parent(poms, PurePosixPath(current), poms[current][0])
        return chain

    for pom_file, (project, pom) in poms.items():
        if not any(True for _ in _plugins(project)):
            continue
        chain = lineage(pom_file)
        properties = {}
        inherited = (None, {"configOptions": {}})
        for ancestor in reversed(chain[1:]):
            parent_project = poms[ancestor][0]
            found = _child(parent_project, "properties")
            for element in [] if found is None else found:
                if isinstance(element.tag, str) and element.text:
                    properties[_local(element.tag)] = element.text.strip()
            version, config = _inherited(parent_project)
            inherited = (version or inherited[0], _merge(inherited[1], config))
        properties.update({"project.basedir": "", "basedir": ""})
        found = _child(project, "properties")
        for element in [] if found is None else found:
            if isinstance(element.tag, str) and element.text:
                properties[_local(element.tag)] = element.text.strip()
        pom_text = pom.read_text(encoding="utf-8", errors="replace")
        line = _line_of(pom_text, _PLUGIN)
        module = PurePosixPath(pom_file).parent
        for version, config in _executions(project, inherited):
            _generate(
                root,
                yaml,
                pom_file,
                line,
                module,
                version,
                config,
                properties,
                servers,
                clients,
                unread,
            )
    for build_file, line, version, config in gradle_executions(root):
        if isinstance(config, str):
            unread.append(OpenApiUnread(build_file, line, config))
            continue
        _generate(
            root,
            yaml,
            build_file,
            line,
            PurePosixPath("."),
            version,
            config,
            {},
            servers,
            clients,
            unread,
        )
    return servers, clients, unread
