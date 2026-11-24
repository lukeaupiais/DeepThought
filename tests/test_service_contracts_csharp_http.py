"""C# HttpClient requests, typed-client base addresses and case-insensitive
ASP.NET Core route matching."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.service_contracts.csharp_http import resolve_url
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
        var api = app.MapGroup("api/catalog");
        api.MapGet("/items/{id:int}", (int id) => id);
        api.MapGet("/catalogbrands", () => 1);
        api.MapPost("/items", () => 1);
    }
}

[ApiController]
[Route("api/[controller]")]
public class OrdersController : ControllerBase
{
    [HttpGet]
    public IActionResult List() => Ok();

    [HttpDelete("{id}")]
    public IActionResult Remove(int id) => Ok();
}
"""

SERVICES = """
using Shop.Web.Services;

namespace Shop.Web;

public static class Setup
{
    public static void Add(IServiceCollection services)
    {
        services.AddHttpClient<CatalogService>(o => o.BaseAddress = new("https+http://catalog-api"));
        services.AddHttpClient<IOrders, OrderingService>(o =>
        {
            o.BaseAddress = new Uri("http://orders-api/");
        });
    }
}
"""

CLIENTS = """
namespace Shop.Web.Services;

public class CatalogService(HttpClient httpClient)
{
    private readonly string remoteServiceBaseUrl = "api/catalog/";
    private const string Brands = "catalogBrands";

    public Task<Item> Get(int id)
    {
        var uri = $"{remoteServiceBaseUrl}items/{id}";
        return httpClient.GetFromJsonAsync<Item>(uri);
    }

    public Task<Brand[]> GetBrands() =>
        httpClient.GetFromJsonAsync<Brand[]>(remoteServiceBaseUrl + Brands);

    public Task Create(Item item) =>
        httpClient.PostAsJsonAsync($"{remoteServiceBaseUrl}items", item);

    public Task Elsewhere()
    {
        // A local client has no typed-client base address.
        using var other = new HttpClient();
        return other.GetAsync("http://catalog-api/api/catalog/items/7");
    }
}

public class OrderingService : IOrders
{
    private readonly HttpClient _client;
    private string _changed = "/api/orders";

    public OrderingService(HttpClient client)
    {
        _client = client;
    }

    public Task List()
    {
        var message = new HttpRequestMessage(HttpMethod.Get, "/api/Orders");
        return _client.SendAsync(message);
    }

    public Task Remove(int id) => _client.DeleteAsync($"api/orders/{id}");

    public Task Changed()
    {
        _changed = "/other";
        return _client.GetAsync(_changed);
    }

    public Task NotAClient(Repository repository) => repository.GetAsync("/api/orders");
}
"""

FILES = {
    "src/Catalog.API/Apis/CatalogApi.cs": CATALOG_API,
    "src/Web/Setup.cs": SERVICES,
    "src/Web/Services/Clients.cs": CLIENTS,
}


@unittest.skipUnless(READY, "tree-sitter-c-sharp is not installed")
class CSharpHttpClientTests(unittest.TestCase):
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
                by_id[res.request_id].line,
                by_id[res.request_id].method,
                by_id[res.request_id].url.replace(PLACEHOLDER, "{?}"),
                res.status,
                paths.get(res.route_id),
            )
            for res in resolutions
        )
        self.assertEqual(
            found,
            [
                (
                    12,
                    "GET",
                    "http://catalog-api/api/catalog/items/{?}",
                    "linked",
                    "GET /api/catalog/items/{id}",
                ),
                (
                    16,
                    "GET",
                    "http://catalog-api/api/catalog/catalogBrands",
                    "linked",
                    "GET /api/catalog/catalogbrands",
                ),
                (
                    19,
                    "POST",
                    "http://catalog-api/api/catalog/items",
                    "linked",
                    "POST /api/catalog/items",
                ),
                (
                    25,
                    "GET",
                    "http://catalog-api/api/catalog/items/7",
                    "linked",
                    "GET /api/catalog/items/{id}",
                ),
                (
                    42,
                    "GET",
                    "http://orders-api/api/Orders",
                    "linked",
                    "GET /api/Orders",
                ),
                (
                    45,
                    "DELETE",
                    "http://orders-api/api/orders/{?}",
                    "linked",
                    "DELETE /api/Orders/{id}",
                ),
                (50, "GET", "http://orders-api/{?}", "unresolved", None),
            ],
        )

    def test_edges_carry_the_client(self):
        clients = {
            r.properties["client"]
            for r in self.output.relationships
            if r.type == "REQUESTS"
        }
        self.assertEqual(clients, {"dotnet-httpclient"})


class ResolveUrlTests(unittest.TestCase):
    def test_resolution(self):
        self.assertEqual(resolve_url("https+http://api", "api/x"), "http://api/api/x")
        self.assertEqual(resolve_url("http://api/v1/", "x"), "http://api/v1/x")
        self.assertEqual(resolve_url("http://api/v1", "x"), "http://api/x")
        self.assertEqual(resolve_url("http://api/v1/", "/x"), "http://api/x")
        self.assertEqual(resolve_url(None, "x/y"), PLACEHOLDER + "/x/y")
        self.assertEqual(resolve_url(None, "/x"), "/x")
        self.assertEqual(resolve_url("http://a", "http://b/c"), "http://b/c")


if __name__ == "__main__":
    unittest.main()
