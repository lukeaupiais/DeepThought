"""Refit interface requests and their base addresses."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.http import PLACEHOLDER

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_c_sharp"
)

CATALOG_API = """
namespace Shop.Catalog;

public static class CatalogApi
{
    public static void Map(IEndpointRouteBuilder app)
    {
        app.MapGet("/products/{id}", (Guid id) => id);
        app.MapGet("/products", () => 1);
        app.MapPost("/products", () => 1);
        app.MapDelete("/basket/{user}", (string user) => user);
    }
}
"""

GLOBALS = """
global using Refit;
global using Shop.Web.Services;
"""

SETUP = """
namespace Shop.Web;

public static class Setup
{
    public static void Add(IServiceCollection services, IConfiguration config)
    {
        services.AddRefitClient<ICatalog>()
            .ConfigureHttpClient(c => c.BaseAddress = new Uri("http://catalog/api"));
        services.AddRefitClient<IBasket>()
            .ConfigureHttpClient(c =>
            {
                c.BaseAddress = new Uri(config["Gateway"]!);
            });
        var direct = RestService.For<IDirect>("http://catalog");
    }
}
"""

SERVICES = """
namespace Shop.Web.Services;

public interface ICatalog
{
    [Get("/products/{id}")]
    Task<Product> Get(Guid id);

    [Get("/products?page={page}")]
    Task<Product[]> List(int page);

    [Post("/products")]
    Task Create([Body] Product product);

    Task<Product> NotARequest(Guid id);
}

public interface IBasket
{
    [Delete("/basket/{user}")]
    Task Remove(string user);
}

public interface IDirect
{
    [Refit.Get("/products/{id}")]
    Task<Product> Get(Guid id);

    [Headers("Accept: application/json")]
    [Post("/products")]
    Task Create(Product product);
}
"""

OTHER = """
using Other.Http;

namespace Shop.Other;

// Get from another namespace in a project without Refit.
public interface IOther
{
    [Get("/products")]
    Task List();
}
"""

FILES = {
    "src/Catalog.API/Catalog.API.csproj": "<Project />",
    "src/Catalog.API/CatalogApi.cs": CATALOG_API,
    "src/Web/Web.csproj": "<Project />",
    "src/Web/GlobalUsing.cs": GLOBALS,
    "src/Web/Setup.cs": SETUP,
    "src/Web/Services/Clients.cs": SERVICES,
    "src/Other/Other.csproj": "<Project />",
    "src/Other/Other.cs": OTHER,
}


@unittest.skipUnless(READY, "tree-sitter-c-sharp is not installed")
class RefitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.tool = ServiceContractTool(root)
        self.output = asyncio.run(self.tool.analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_requests(self):
        _, routes, requests, _, resolutions = self.tool.last_facts
        paths = {r.id: f"{' '.join(r.methods)} {r.path}" for r in routes}
        by_id = {r.id: r for r in requests}
        found = sorted(
            (
                by_id[res.request_id].file.rpartition("/")[2],
                by_id[res.request_id].line,
                by_id[res.request_id].method,
                by_id[res.request_id].url.replace(PLACEHOLDER, "{?}"),
                res.status,
                paths.get(res.route_id),
            )
            for res in resolutions
            if by_id[res.request_id].client == "refit"
        )
        self.assertEqual(
            found,
            [
                # Refit appends the path to the base address, path included.
                (
                    "Clients.cs",
                    6,
                    "GET",
                    "http://catalog/api/products/{id}",
                    "unresolved",
                    None,
                ),
                (
                    "Clients.cs",
                    9,
                    "GET",
                    "http://catalog/api/products?page={page}",
                    "unresolved",
                    None,
                ),
                (
                    "Clients.cs",
                    12,
                    "POST",
                    "http://catalog/api/products",
                    "unresolved",
                    None,
                ),
                # A base that is not a literal leaves the path as written.
                (
                    "Clients.cs",
                    20,
                    "DELETE",
                    "/basket/{user}",
                    "linked",
                    "DELETE /basket/{user}",
                ),
                (
                    "Clients.cs",
                    26,
                    "GET",
                    "http://catalog/products/{id}",
                    "linked",
                    "GET /products/{id}",
                ),
                (
                    "Clients.cs",
                    30,
                    "POST",
                    "http://catalog/products",
                    "linked",
                    "POST /products",
                ),
            ],
        )

    def test_edges_come_from_the_interface_methods(self):
        callers = sorted(
            r.source_match["qualified_name"].split("::")[2]
            for r in self.output.relationships
            if r.type == "REQUESTS" and r.properties["client"] == "refit"
        )
        self.assertEqual(
            callers,
            [
                "Shop.Web.Services.IBasket.Remove",
                "Shop.Web.Services.IDirect.Create",
                "Shop.Web.Services.IDirect.Get",
            ],
        )


if __name__ == "__main__":
    unittest.main()
