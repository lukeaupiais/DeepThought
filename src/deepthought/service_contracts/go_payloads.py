"""The types a Go HTTP handler decodes its request body into and encodes
its response from.

The Go type checker (``go_facts``) gives, for each method call, the package
and receiver type declaring the method and the named type of its
arguments (through pointers and slices) with the declaration's position.
A handler's call of one of these methods carries that argument's type:

- request: gin's ``Context.Bind``/``ShouldBind``/``BindJSON``/
  ``ShouldBindJSON``, echo's ``Context.Bind``, fiber's ``Ctx.BodyParser``
  (v3: ``Ctx.Bind().Body`` and its JSON, XML, Form, MultipartForm,
  MsgPack and CBOR)
  (the first argument), and ``encoding/json``'s ``Decoder.Decode`` on a
  decoder made by ``json.NewDecoder(r.Body)`` where ``r`` is the handler's
  ``*http.Request`` parameter, and go-chi/render's ``Bind``, ``Decode``
  (the second argument, with the handler's request first) and
  ``DecodeJSON`` (with the request's ``Body`` first);
- response: gin's ``Context.JSON`` (and ``IndentedJSON``, ``PureJSON``,
  ``SecureJSON``, ``AsciiJSON``) and echo's ``Context.JSON``/``JSONPretty``
  (the second argument), fiber's ``Ctx.JSON`` (the first), and
  ``Encoder.Encode`` on ``json.NewEncoder(w)`` where ``w`` is the
  handler's ``http.ResponseWriter`` parameter, and go-chi/render's
  ``JSON``, ``Render`` and ``Respond`` (the third argument, with the
  handler's response writer first).

The decoder and encoder may be made in the call itself or held in a
variable the handler assigns once from such a call. Calls in functions the
handler calls are not followed.
"""

from .folding import text

_GIN = "github.com/gin-gonic/gin"
_ECHO = ("github.com/labstack/echo/v4", "github.com/labstack/echo")
_FIBER = ("github.com/gofiber/fiber/v2", "github.com/gofiber/fiber/v3")

# (declaring package, receiver type, method) -> (direction, argument index)
_CALLS = {
    **{
        (_GIN, "Context", method): ("request", 0)
        for method in ("Bind", "ShouldBind", "BindJSON", "ShouldBindJSON")
    },
    **{
        (_GIN, "Context", method): ("response", 1)
        for method in ("JSON", "IndentedJSON", "PureJSON", "SecureJSON", "AsciiJSON")
    },
    **{(echo, "Context", "Bind"): ("request", 0) for echo in _ECHO},
    **{
        (echo, "Context", method): ("response", 1)
        for echo in _ECHO
        for method in ("JSON", "JSONPretty")
    },
    **{(fiber, "Ctx", "BodyParser"): ("request", 0) for fiber in _FIBER},
    **{(fiber, "Ctx", "JSON"): ("response", 0) for fiber in _FIBER},
    # Fiber v3 binds the body through c.Bind(): Body by the content type,
    # or one format.
    **{
        ("github.com/gofiber/fiber/v3", "Bind", method): ("request", 0)
        for method in (
            "Body",
            "JSON",
            "XML",
            "Form",
            "MultipartForm",
            "MsgPack",
            "CBOR",
        )
    },
}
_CHI_RENDER = "github.com/go-chi/render"
# go-chi/render's package functions: (declaring package, function) ->
# (direction, argument index, handler parameter type the first argument
# must be, field of it read). render.Render and render.Bind take their
# value as a Renderer or Binder; the argument's static type is the body.
_FUNCTIONS = {
    **{
        (_CHI_RENDER, name): ("response", 2, "http.ResponseWriter", None)
        for name in ("JSON", "Render", "Respond")
    },
    **{
        (_CHI_RENDER, name): ("request", 1, "*http.Request", None)
        for name in ("Bind", "Decode")
    },
    (_CHI_RENDER, "DecodeJSON"): ("request", 1, "*http.Request", "Body"),
}
# encoding/json's Decoder.Decode and Encoder.Encode: what they read and
# write is checked on the handler's parameters.
_JSON = {("Decoder", "Decode"): "request", ("Encoder", "Encode"): "response"}


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _parameters(function, content):
    """{name: type text} of a Go function's parameters."""
    found = {}
    parameters = function.child_by_field_name("parameters")
    for item in [] if parameters is None else parameters.named_children:
        if item.type != "parameter_declaration":
            continue
        written = item.child_by_field_name("type")
        for name in item.children_by_field_name("name"):
            found[text(name, content)] = (
                "" if written is None else text(written, content).replace(" ", "")
            )
    return found


def _json_call(node, content, function_name):
    """The argument of ``json.<function_name>(argument)``, else None."""
    if node is None or node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    if function is None or function.type != "selector_expression":
        return None
    if text(function, content) != f"json.{function_name}":
        return None
    arguments = node.child_by_field_name("arguments")
    found = [] if arguments is None else arguments.named_children
    return found[0] if len(found) == 1 else None


def _assigned_once(function, content):
    """{variable: value node} of the handler's variables assigned exactly
    once (``x := v`` or ``x = v`` with one name)."""
    found, counts = {}, {}
    for node in _walk(function):
        if node.type not in ("short_var_declaration", "assignment_statement"):
            continue
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            continue
        names = [n for n in left.named_children]
        values = [v for v in right.named_children]
        for index, name in enumerate(names):
            if name.type != "identifier":
                continue
            key = text(name, content)
            counts[key] = counts.get(key, 0) + 1
            if len(names) == len(values):
                found[key] = values[index]
    return {k: v for k, v in found.items() if counts[k] == 1}


def _handler_parameter(argument, function, content, parameter_type, field):
    """Whether ``argument`` is the handler's parameter of ``parameter_type``
    (followed by ``.field`` when given)."""
    if argument is not None and field is not None:
        if argument.type != "selector_expression":
            return False
        if text(argument.child_by_field_name("field"), content) != field:
            return False
        argument = argument.child_by_field_name("operand")
    if argument is None or argument.type != "identifier":
        return False
    return _parameters(function, content).get(text(argument, content)) == (
        parameter_type
    )


def _json_stream(operand, function, content, made_by, parameter_type, field):
    """Whether the decoder or encoder ``operand`` is ``json.<made_by>(x)``,
    directly or through a variable assigned once, with ``x`` the handler's
    parameter of ``parameter_type`` (followed by ``.field`` when given)."""
    if operand is not None and operand.type == "identifier":
        operand = _assigned_once(function, content).get(text(operand, content))
    argument = _json_call(operand, content, made_by)
    return argument is not None and _handler_parameter(
        argument, function, content, parameter_type, field
    )


def handler_payloads(function, content, calls, bound=None):
    """[(direction, argument type (package, name), declaration (file, line)
    or None)] of the go_facts ``calls`` in the handler ``function`` (a
    tree-sitter function or method declaration, or a func literal) whose
    argument carries the request body or the response. ``bound`` maps the
    references of a helper's interface parameters to the (named type,
    declaration) of what its caller passes them: an interface argument
    that is such a parameter carries that type."""
    starts = {}
    for node in _walk(function):
        if node.type == "call_expression":
            starts.setdefault(
                (node.start_point.row + 1, node.start_point.column), []
            ).append(node)
    found = []
    for call in calls:
        nodes = [
            n
            for n in starts.get((call.line, call.column), ())
            if (f := n.child_by_field_name("function")) is not None
            and f.type == "selector_expression"
            and text(f.child_by_field_name("field"), content) == call.method
        ]
        if not nodes:
            continue
        if call.receiver is None:
            if (call.declared, call.method) not in _FUNCTIONS:
                continue
            direction, index, parameter_type, field = _FUNCTIONS[
                (call.declared, call.method)
            ]
            arguments = nodes[0].child_by_field_name("arguments")
            first = (
                None
                if arguments is None
                else next(iter(arguments.named_children), None)
            )
            if not _handler_parameter(first, function, content, parameter_type, field):
                continue
        elif (key := (call.declared, call.receiver[1], call.method)) in _CALLS:
            direction, index = _CALLS[key]
        elif call.declared == "encoding/json" and key[1:] in _JSON:
            direction, index = _JSON[key[1:]], 0
            operand = (
                nodes[0].child_by_field_name("function").child_by_field_name("operand")
            )
            if direction == "request":
                ok = _json_stream(
                    operand,
                    function,
                    content,
                    "NewDecoder",
                    "*http.Request",
                    "Body",
                )
            else:
                ok = _json_stream(
                    operand,
                    function,
                    content,
                    "NewEncoder",
                    "http.ResponseWriter",
                    None,
                )
            if not ok:
                continue
        else:
            continue
        if index < len(call.interfaces) and call.interfaces[index]:
            given = (bound or {}).get(
                call.refs[index] if index < len(call.refs) else None
            )
            if given is not None:
                found.append((direction, given[0], given[1]))
                continue
            # An interface value (body any): what it holds is not known.
            found.append((direction, None, None))
            continue
        if index >= len(call.types) or call.types[index] is None:
            continue
        declaration = (
            call.declarations[index] if index < len(call.declarations) else None
        )
        found.append((direction, call.types[index], declaration))
    return found
