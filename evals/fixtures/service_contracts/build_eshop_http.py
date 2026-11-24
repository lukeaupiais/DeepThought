"""Build the eShop HTTP contract-link ground truth JSON.

dotnet/eShop's WebApp and WebhookClient call Catalog.API, Ordering.API and
Webhooks.API over HTTP through typed HttpClients whose BaseAddress names the
Aspire resource (https+http://catalog-api). Labelled by reading the repository
at its pinned commit by hand, separately from the C# readers: their code, tests
and reference were not consulted. Run
`CORPORA=<clones> python3 build_eshop_http.py <outdir>`.
"""

import sys

from corpus_builder import Corpus

OUT = sys.argv[1]
CATALOG_CLIENT = "src/WebAppComponents/Services/CatalogService.cs"
ORDERING_CLIENT = "src/WebApp/Services/OrderingService.cs"
WEBHOOKS_CLIENT = "src/WebhookClient/Services/WebHooksClient.cs"
CATALOG_API = "src/Catalog.API/Apis/CatalogApi.cs"
ORDERS_API = "src/Ordering.API/Apis/OrdersApi.cs"
WEBHOOKS_API = "src/Webhooks.API/Apis/WebHooksApi.cs"

eshop = Corpus(
    "eshop",
    corpus="eshop-http",
    repo="https://github.com/dotnet/eShop",
    license="MIT",
    languages=["C#"],
    sources=[
        "src/eShop.AppHost/AppHost.cs AddDotnetProject resource names (catalog-api, "
        "ordering-api, webhooks-api, webapp, webhooksclient)",
        "src/WebApp/Extensions/Extensions.cs AddHttpClient<CatalogService>(https+http://catalog-api, "
        "AddApiVersion(2.0)) and AddHttpClient<OrderingService>(https+http://ordering-api, AddApiVersion(1.0))",
        "src/WebhookClient/Extensions/Extensions.cs AddHttpClient<WebhooksClient>(http://webhooks-api)",
        "minimal API route groups: CatalogApi.cs MapGroup(api/catalog) with v1/v2 groups, "
        "OrdersApi.cs MapGroup(api/orders), WebHooksApi.cs MapGroup(/api/webhooks)",
        "manual reading of every HttpClient call at the pinned commit",
    ],
    services=[
        {"name": "catalog-api", "root": "src/Catalog.API"},
        {"name": "ordering-api", "root": "src/Ordering.API"},
        {"name": "webhooks-api", "root": "src/Webhooks.API"},
        {"name": "webapp", "root": "src/WebApp"},
        {"name": "webapp", "root": CATALOG_CLIENT},
        {"name": "webhooksclient", "root": "src/WebhookClient"},
    ],
    notes=[
        "client is the line of the HttpClient call (GetFromJsonAsync, PostAsJsonAsync, SendAsync). "
        "Most URLs are interpolated a line above from a remoteServiceBaseUrl field; the path is "
        "the field plus the literal suffix, with the query string dropped.",
        "server is the line of the Map<Verb>( call in the minimal API group.",
        "WebAppComponents/Services/CatalogService.cs is a shared-library class registered as a "
        "typed client only by WebApp, so its calls belong to webapp. HybridApp has its own "
        "CatalogService.",
        "Catalog.API is versioned by the api-version query parameter. WebApp's catalog client "
        "adds api-version=2.0 (AddApiVersion(2.0)), so GET /items and "
        "/items/withsemanticrelevance resolve to the v2 handlers, not the v1 ones.",
        "ASP.NET Core route matching is case-insensitive: catalogBrands matches /catalogbrands, "
        "catalogTypes /catalogtypes, and /api/Orders/ the api/orders group.",
        "OrderingService.CreateOrder posts through new HttpRequestMessage(HttpMethod.Post, "
        "remoteServiceBaseUrl) and SendAsync; the client line is the SendAsync line.",
    ],
)

LINKS = [
    ("webapp", "catalog-api", "GET /api/catalog/items/{id}", CATALOG_CLIENT,
     "return httpClient.GetFromJsonAsync<CatalogItem>(uri);", CATALOG_API, 'api.MapGet("/items/{id:int}", GetItemById)',
     "GetCatalogItem: $\"{remoteServiceBaseUrl}items/{id}\""),
    ("webapp", "catalog-api", "GET /api/catalog/items", CATALOG_CLIENT,
     "await httpClient.GetFromJsonAsync<CatalogResult>(uri);", CATALOG_API, 'v2.MapGet("/items", GetAllItems)',
     "GetCatalogItems: GetAllCatalogItemsUri -> {baseUri}items?...; api-version 2.0"),
    ("webapp", "catalog-api", "GET /api/catalog/items/by", CATALOG_CLIENT,
     "GetFromJsonAsync<List<CatalogItem>>(uri);", CATALOG_API, 'api.MapGet("/items/by", GetItemsByIds)',
     "GetCatalogItems(ids): items/by?ids=..."),
    ("webapp", "catalog-api", "GET /api/catalog/items/withsemanticrelevance", CATALOG_CLIENT,
     "httpClient.GetFromJsonAsync<CatalogResult>(url);", CATALOG_API,
     'v2.MapGet("/items/withsemanticrelevance", GetItemsBySemanticRelevance)',
     "GetCatalogItemsWithSemanticRelevance: items/withsemanticrelevance?text=...; api-version 2.0"),
    ("webapp", "catalog-api", "GET /api/catalog/catalogbrands", CATALOG_CLIENT,
     "GetFromJsonAsync<CatalogBrand[]>(uri);", CATALOG_API, 'api.MapGet("/catalogbrands",',
     "GetBrands: catalogBrands"),
    ("webapp", "catalog-api", "GET /api/catalog/catalogtypes", CATALOG_CLIENT,
     "GetFromJsonAsync<CatalogItemType[]>(uri);", CATALOG_API, 'api.MapGet("/catalogtypes",',
     "GetTypes: catalogTypes"),
    ("webapp", "catalog-api", "GET /api/catalog/items/facets", CATALOG_CLIENT,
     "GetFromJsonAsync<CatalogFacets>(uri);", CATALOG_API, 'api.MapGet("/items/facets", GetCatalogFacets)',
     "GetCatalogFacets: items/facets?..."),
    ("webapp", "ordering-api", "GET /api/orders", ORDERING_CLIENT,
     "GetFromJsonAsync<OrderRecord[]>(remoteServiceBaseUrl)", ORDERS_API, 'api.MapGet("/", GetOrdersByUserAsync);',
     "GetOrders: /api/Orders/"),
    ("webapp", "ordering-api", "POST /api/orders", ORDERING_CLIENT,
     "return httpClient.SendAsync(requestMessage);", ORDERS_API, 'api.MapPost("/", CreateOrderAsync);',
     "CreateOrder: HttpRequestMessage(HttpMethod.Post, /api/Orders/)"),
    ("webhooksclient", "webhooks-api", "POST /api/webhooks", WEBHOOKS_CLIENT,
     'client.PostAsJsonAsync("/api/webhooks", payload)', WEBHOOKS_API, 'api.MapPost("/",',
     "AddWebHookAsync"),
    ("webhooksclient", "webhooks-api", "GET /api/webhooks", WEBHOOKS_CLIENT,
     'GetFromJsonAsync<IEnumerable<WebhookResponse>>("/api/webhooks")', WEBHOOKS_API, 'api.MapGet("/",',
     "LoadWebhooks"),
]
for frm, to, contract, cfile, cpat, sfile, spat, why in LINKS:
    eshop.link("http", frm, to, contract, eshop.loc(cfile, cpat), eshop.loc(sfile, spat),
               f"{why} -> {contract}")

eshop.dc({"file": "tests"}, "test clients and in-process host calls")
eshop.dc({"file": "e2e"}, "Playwright end-to-end tests")
eshop.dc({"file": "src/HybridApp"},
         "MAUI app; base address is a localhost port of the mobile-bff YARP proxy, not a named resource")
eshop.dc({"file": "src/ClientApp"},
         "MAUI app; endpoints come from user settings at runtime (SettingsService preferences)")
eshop.dc({"file": "src/Webhooks.API/Services/WebhooksSender.cs"}, "posts to subscriber-registered URLs")
eshop.dc({"file": "src/Webhooks.API/Services/GrantUrlTesterService.cs"}, "probes subscriber-registered URLs")
eshop.dc({"file": "src/eShop.ServiceDefaults/HttpClientExtensions.cs"}, "delegating handler, not a request site")
eshop.write(OUT)
