"""ASP.NET Core routes: attribute-routed controllers and minimal APIs."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_c_sharp"
)

CONTROLLERS = """
using Microsoft.AspNetCore.Mvc;

namespace Shop.Web;

[ApiController]
[Route("api/[controller]")]
public class OrdersController : ControllerBase
{
    [HttpGet]
    public IActionResult List() => Ok();

    [HttpGet("{id:int}")]
    public IActionResult Get(int id) => Ok();

    [HttpPost("/absolute/[action]")]
    public Task<IActionResult> CreateAsync() => null;

    [Route("search/{term?}")]
    public IActionResult Search(string term) => Ok();

    public IActionResult NotAnAction() => Ok();
}

[Route("[controller]/[action]")]
public class AccountController : Controller
{
    [HttpGet]
    public IActionResult Login() => View();
}

// Conventionally routed: no template on either level.
public class HomeController : Controller
{
    [HttpGet]
    public IActionResult Index() => View();
}

public class Login : EndpointBaseAsync.WithRequest<Req>.WithActionResult<Res>
{
    [HttpPost("api/authenticate")]
    public override Task<ActionResult<Res>> HandleAsync(Req request) => null;
}

[Route(Routes.Dynamic)]
public class DynamicController : ControllerBase
{
    [HttpGet]
    public IActionResult Get() => Ok();
}
"""

MINIMAL = """
var app = builder.Build();
app.MapGet("/health", () => "ok");
var items = app.MapGroup("/api/items").WithTags("items");
items.MapGet("{id}", (int id) => id);
items.MapDelete("/{id}", (int id) => id);
app.MapGroup("api").MapGroup("v2").MapPost("orders", () => 1);
app.MapMethods("/both", new[] { "GET", "HEAD" }, () => 1);
app.MapMethods("/check", [HttpMethods.Options], () => 1);
app.MapMethods("/verbs", verbs, () => 1);

public static class Extensions
{
    public static void MapApi(this RouteGroupBuilder group)
    {
        group.MapPut("users/{id}", (int id) => id);
        // A project's own overload taking the handler first is not a template.
        group.MapPatch(Handler, "ignored");
    }
}
"""

FILES = {
    "shop/Controllers.cs": CONTROLLERS,
    "shop/Program.cs": MINIMAL,
}


@unittest.skipUnless(READY, "tree-sitter-c-sharp is not installed")
class AspNetRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.output = asyncio.run(ServiceContractTool(root).analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def routes(self, framework):
        return sorted(
            (
                tuple(n.properties["methods"]),
                n.properties["path"],
                bool(n.properties.get("base_unknown")),
            )
            for n in self.output.nodes
            if "HttpRoute" in n.labels and n.properties["framework"] == framework
        )

    def test_controllers(self):
        self.assertEqual(
            self.routes("aspnet-controller"),
            [
                (("*",), "/api/Orders/search/{term}", False),
                (("GET",), "/Account/Login", False),
                (("GET",), "/api/Orders", False),
                (("GET",), "/api/Orders/{id}", False),
                (("POST",), "/absolute/Create", False),
                (("POST",), "/api/authenticate", False),
            ],
        )
        handlers = {
            r.source_match["qualified_name"].split("::")[2]
            for r in self.output.relationships
            if r.type == "HANDLES"
            and r.properties["rule_id"] == "service-contracts.aspnet-controller-route"
        }
        self.assertIn("Shop.Web.OrdersController.Get", handlers)

    def test_minimal_apis(self):
        self.assertEqual(
            self.routes("aspnet-minimal-api"),
            [
                (("DELETE",), "/api/items/{id}", False),
                (("GET",), "/api/items/{id}", False),
                (("GET",), "/health", False),
                (("GET", "HEAD"), "/both", False),
                (("OPTIONS",), "/check", False),
                (("POST",), "/api/v2/orders", False),
                (("PUT",), "/users/{id}", True),
            ],
        )

    def test_unread(self):
        self.assertEqual(
            self.output.metadata.custom_metrics["unread"],
            {"route:methods_not_literal": 1, "route:path_not_literal": 2},
        )


if __name__ == "__main__":
    unittest.main()
