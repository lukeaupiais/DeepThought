"""Build the eShop gRPC contract-link ground truth JSON.

dotnet/eShop's Basket.API serves the BasketApi.Basket gRPC service from C#; the
WebApp (Blazor) and ClientApp (.NET MAUI) call it through protoc stubs. Labelled
by reading the repository at its pinned commit and both .proto copies by hand,
separately from the C# readers: their code, tests and reference were not
consulted. Run `CORPORA=<clones> python3 build_eshop_grpc.py <outdir>`.
"""

import sys

from corpus_builder import Corpus

OUT = sys.argv[1]
SERVICE = "BasketApi.Basket"
PROTO = "src/Basket.API/Proto/basket.proto"
RPCS = {
    "GetBasket": "rpc GetBasket(GetBasketRequest) returns (CustomerBasketResponse) {}",
    "UpdateBasket": "rpc UpdateBasket(UpdateBasketRequest) returns (CustomerBasketResponse) {}",
    "DeleteBasket": "rpc DeleteBasket(DeleteBasketRequest) returns (DeleteBasketResponse) {}",
}
SERVER = "src/Basket.API/Grpc/BasketService.cs"
CLIENTS = {
    "webapp": (
        "src/WebApp/Services/BasketService.cs",
        {
            "GetBasket": "await basketClient.GetBasketAsync(",
            "UpdateBasket": "await basketClient.UpdateBasketAsync(",
            "DeleteBasket": "await basketClient.DeleteBasketAsync(",
        },
    ),
    "clientapp": (
        "src/ClientApp/Services/Basket/BasketService.cs",
        {
            "GetBasket": ".GetBasketAsync(new GetBasketRequest()",
            "UpdateBasket": ".UpdateBasketAsync(updateBasketRequest",
            "DeleteBasket": "GetBasketClient().DeleteBasketAsync(",
        },
    ),
}

eshop = Corpus(
    "eshop",
    corpus="eshop-grpc",
    repo="https://github.com/dotnet/eShop",
    license="MIT",
    languages=["C#"],
    sources=[
        "src/Basket.API/Proto/basket.proto and src/ClientApp/Services/Basket/Protos/basket.proto "
        "(package BasketApi, service Basket)",
        "src/Basket.API/Program.cs app.MapGrpcService<BasketService>() and "
        "src/WebApp/Extensions/Extensions.cs AddGrpcClient<Basket.BasketClient>(http://basket-api)",
        "manual reading of every stub call and server override at the pinned commit",
    ],
    services=[
        {"name": "basket-api", "root": "src/Basket.API"},
        {"name": "webapp", "root": "src/WebApp"},
        {"name": "clientapp", "root": "src/ClientApp"},
    ],
    notes=[
        "server is the line of `public override async Task<...> <Rpc>(` in "
        "Basket.API's BasketService : Basket.BasketBase.",
        "client is the line of the <Rpc>Async stub call. WebApp injects "
        "Basket.BasketClient (registered with AddGrpcClient); ClientApp builds "
        "new Basket.BasketClient(GrpcChannel.ForAddress(...)) in GetBasketClient() "
        "and chains the call, so its GetBasket and UpdateBasket client lines are the "
        "lines holding .<Rpc>Async(, one below GetBasketClient().",
        "ClientApp compiles its own copy of basket.proto under another csharp_namespace; "
        "the wire service is the same BasketApi.Basket.",
        "tests/ is don't-care: Basket.UnitTests calls the server methods in process, "
        "not through a stub. BasketMockService.cs is a mock with no RPC.",
    ],
)
for rpc, signature in RPCS.items():
    contract_def = eshop.loc(PROTO, signature)
    server = eshop.loc(SERVER, f"> {rpc}(")
    for name, (path, calls) in CLIENTS.items():
        eshop.link(
            "grpc", name, "basket-api", f"{SERVICE}/{rpc}",
            eshop.loc(path, calls[rpc]),
            server,
            f"{calls[rpc].strip()} -> {SERVICE}/{rpc} ({signature})",
            streaming="unary",
            contract_def=contract_def,
        )
eshop.dc({"file": "tests"}, "in-process calls of server methods, not RPC clients")
eshop.dc({"file": "src/ClientApp/Services/Basket/BasketMockService.cs"}, "mock basket service, no RPC")
eshop.write(OUT)
