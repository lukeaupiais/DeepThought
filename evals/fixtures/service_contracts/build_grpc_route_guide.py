"""Build the gRPC route_guide contract-link ground truth JSONs.

grpc's route_guide example has one rpc of each streaming kind, in every
language. Labelled by reading each repository at its pinned commit and its
.proto by hand, separately from the gRPC readers (#162, #164): their code and
tests were not consulted. Run
`CORPORA=<clones> python3 build_grpc_route_guide.py <outdir>`.

Each link carries ``streaming``: unary, server, client or bidi, from the
``stream`` keywords of the rpc's request and response in the .proto.
"""

import os
import sys

from corpus_builder import C, Corpus

OUT = sys.argv[1]

SERVICE = "routeguide.RouteGuide"
# rpc -> (streaming kind, proto signature as written)
RPCS = {
    "GetFeature": ("unary", "rpc GetFeature(Point) returns (Feature) {}"),
    "ListFeatures": ("server", "rpc ListFeatures(Rectangle) returns (stream Feature) {}"),
    "RecordRoute": ("client", "rpc RecordRoute(stream Point) returns (RouteSummary) {}"),
    "RouteChat": ("bidi", "rpc RouteChat(stream RouteNote) returns (stream RouteNote) {}"),
}


def lower(name):
    return name[0].lower() + name[1:]


def others(corpus, root, keep, why):
    """Every entry of root/ but ``keep``, as directory-wide don't-cares."""
    for name in sorted(os.listdir(f"{C}/{corpus.dir}/{root}")):
        path = f"{root}/{name}"
        if name not in keep and os.path.isdir(f"{C}/{corpus.dir}/{path}"):
            corpus.dc({"file": path.split("/", 1)[1] if "/" in path else path}, why)


def label(corpus, proto, client, server, client_pattern, server_pattern, frm, to, **extra):
    for rpc, (kind, signature) in RPCS.items():
        contract_def = corpus.loc(proto, signature)
        server_at = corpus.loc(server, server_pattern(rpc))
        sites = client_pattern(rpc)
        for n, pattern in sites:
            corpus.link(
                "grpc", frm, to, f"{SERVICE}/{rpc}",
                corpus.loc(client, pattern, n),
                server_at,
                f"{pattern.strip()} -> {SERVICE}/{rpc} ({signature})",
                streaming=kind,
                contract_def=contract_def,
                **{k: v(rpc) for k, v in extra.items()},
            )


def strip(root, where):
    return {**where, "file": where["file"][len(root) + 1:]}


# ---------------------------------------------------------------- grpc-go
go = Corpus(
    "grpc-go",
    corpus="grpc-go-route-guide",
    repo="https://github.com/grpc/grpc-go",
    root="examples/route_guide",
    license="Apache-2.0",
    languages=["Go"],
    sources=["manual reading of examples/route_guide at the pinned commit"],
    services=[
        {"name": "route-guide-client", "root": "client"},
        {"name": "route-guide-server", "root": "server"},
    ],
    notes=[
        "Paths are relative to the corpus root, examples/route_guide. The server registers "
        "pb.RegisterRouteGuideServer(grpcServer, newServer()); newServer returns "
        "*routeGuideServer, whose methods serve the four rpcs.",
        "client is the line of the stub call client.<Rpc>(ctx, ...) on pb.NewRouteGuideClient(conn); "
        "server is the line of func (s *routeGuideServer) <Rpc>.",
        "routeguide/ holds protoc output (route_guide.pb.go, route_guide_grpc.pb.go): "
        "don't-care, it is the stub and the service descriptor, not a caller.",
    ],
)
GO = "examples/route_guide"
label(
    go,
    f"{GO}/routeguide/route_guide.proto",
    f"{GO}/client/client.go",
    f"{GO}/server/server.go",
    lambda rpc: [(None, f":= client.{rpc}(ctx")],
    lambda rpc: f"func (s *routeGuideServer) {rpc}(",
    "route-guide-client",
    "route-guide-server",
)
for link in go.links:
    for key in ("client", "server", "contract_def"):
        link[key] = strip(GO, link[key])
go.dc({"file": "routeguide"}, "protoc output: stubs and descriptors, not callers")


# --------------------------------------------------------------- grpc-java
JAVA = "examples/src/main/java/io/grpc/examples/routeguide"
java = Corpus(
    "grpc-java",
    corpus="grpc-java-route-guide",
    repo="https://github.com/grpc/grpc-java",
    root="examples/src/main",
    license="Apache-2.0",
    languages=["Java"],
    sources=["manual reading of examples/src/main (routeguide and proto) at the pinned commit"],
    services=[
        {"name": "route-guide-client", "root": "java/io/grpc/examples/routeguide/RouteGuideClient.java"},
        {"name": "route-guide-server", "root": "java/io/grpc/examples/routeguide/RouteGuideServer.java"},
    ],
    notes=[
        "Paths are relative to the corpus root, examples/src/main. The client and the server "
        "are files of one package, so each service's root is its file.",
        "Stubs come from RouteGuideGrpc.newBlockingStub(channel) (getFeature, listFeatures) and "
        "RouteGuideGrpc.newStub(channel) (recordRoute, routeChat). The server is "
        "RouteGuideService extends RouteGuideGrpc.RouteGuideImplBase, added with "
        "serverBuilder.addService(new RouteGuideService(features)).",
        "Every other example package under java/io/grpc/examples is don't-care: those examples "
        "are not labelled here.",
    ],
)
CALLS = {
    "GetFeature": "feature = blockingStub.getFeature(request);",
    "ListFeatures": "features = blockingStub.listFeatures(request);",
    "RecordRoute": "asyncStub.recordRoute(responseObserver);",
    "RouteChat": "asyncStub.routeChat(new StreamObserver<RouteNote>() {",
}
SERVES = {
    "GetFeature": "public void getFeature(Point request, StreamObserver<Feature> responseObserver) {",
    "ListFeatures": "public void listFeatures(Rectangle request, StreamObserver<Feature> responseObserver) {",
    "RecordRoute": "public StreamObserver<Point> recordRoute(final StreamObserver<RouteSummary> responseObserver) {",
    "RouteChat": "public StreamObserver<RouteNote> routeChat(final StreamObserver<RouteNote> responseObserver) {",
}
label(
    java,
    "examples/src/main/proto/route_guide.proto",
    f"{JAVA}/RouteGuideClient.java",
    f"{JAVA}/RouteGuideServer.java",
    lambda rpc: [(None, CALLS[rpc])],
    lambda rpc: SERVES[rpc],
    "route-guide-client",
    "route-guide-server",
)
for link in java.links:
    for key in ("client", "server", "contract_def"):
        link[key] = strip("examples/src/main", link[key])
for name in sorted(os.listdir(f"{C}/grpc-java/examples/src/main/java/io/grpc/examples")):
    if name != "routeguide":
        java.dc({"file": f"java/io/grpc/examples/{name}"}, "another example, not labelled")


# --------------------------------------------------------------- grpc-node
node = Corpus(
    "grpc-node",
    corpus="grpc-node-route-guide",
    repo="https://github.com/grpc/grpc-node",
    root="examples",
    license="Apache-2.0",
    languages=["JavaScript (Node, @grpc/grpc-js)"],
    sources=["manual reading of examples/routeguide and examples/protos at the pinned commit"],
    services=[
        {"name": "route-guide-client", "root": "routeguide/static_codegen/route_guide_client.js"},
        {"name": "route-guide-client", "root": "routeguide/dynamic_codegen/route_guide_client.js"},
        {"name": "route-guide-server", "root": "routeguide/static_codegen/route_guide_server.js"},
        {"name": "route-guide-server", "root": "routeguide/dynamic_codegen/route_guide_server.js"},
    ],
    notes=[
        "Paths are relative to the corpus root, examples. Two copies of one example: "
        "dynamic_codegen loads ../../protos/route_guide.proto with @grpc/proto-loader "
        "(new routeguide.RouteGuide(...), addService(routeguide.RouteGuide.service, ...)); "
        "static_codegen requires protoc output (new services.RouteGuideClient(...), "
        "addService(services.RouteGuideService, ...)).",
        "Both clients dial localhost:50051 and either server listens there, so the two "
        "copies are one client service and one server service; each service lists both files.",
        "client is the line of client.<rpc>(...); runGetFeature calls getFeature twice. "
        "server is the handler function's declaration; server_alternatives is its entry in "
        "the addService map.",
        "static_codegen/route_guide_grpc_pb.js and route_guide_pb.js are protoc output: "
        "don't-care. Every other example directory is don't-care: not labelled here.",
    ],
)
for copy in ("static_codegen", "dynamic_codegen"):
    base = f"examples/routeguide/{copy}"
    before = len(node.links)
    label(
        node,
        "examples/protos/route_guide.proto",
        f"{base}/route_guide_client.js",
        f"{base}/route_guide_server.js",
        lambda rpc: (
            [(1, f"client.{lower(rpc)}(point1"), (1, f"client.{lower(rpc)}(point2")]
            if rpc == "GetFeature"
            else [(1, f"client.{lower(rpc)}(")]
        ),
        lambda rpc: f"function {lower(rpc)}(call",
        "route-guide-client",
        "route-guide-server",
        server_alternatives=lambda rpc, base=base: [
            node.loc(f"{base}/route_guide_server.js", f"    {lower(rpc)}: {lower(rpc)}")
        ],
    )
    for link in node.links[before:]:
        for key in ("client", "server", "contract_def"):
            link[key] = strip("examples", link[key])
        link["server_alternatives"] = [strip("examples", s) for s in link["server_alternatives"]]
        link["codegen"] = copy.split("_")[0]
for name in ("routeguide/static_codegen/route_guide_grpc_pb.js", "routeguide/static_codegen/route_guide_pb.js"):
    node.dc({"file": name}, "protoc output: stubs and messages, not callers")
others(node, "examples", {"routeguide", "protos"}, "another example, not labelled")

for corpus in (go, java, node):
    corpus.write(OUT)
