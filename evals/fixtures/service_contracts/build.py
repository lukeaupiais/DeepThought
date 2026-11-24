"""Build contract-link ground truth JSONs. Line numbers are located by
exact substring match at the pinned checkout and asserted unique (or nth)."""
import json, os, subprocess, sys

C = os.environ.get("CORPORA", "corpora")
OUT = sys.argv[1]


def head(repo):
    return subprocess.check_output(['git', '-C', f'{C}/{repo}', 'rev-parse', 'HEAD'], text=True).strip()


class Corpus:
    def __init__(self, repo_dir, **meta):
        self.dir = repo_dir
        self.meta = meta
        self.links = []
        self.dont_care = []

    def loc(self, f, pat, n=None):
        p = f'{C}/{self.dir}/{f}'
        lines = open(p, encoding='utf-8', errors='replace').read().split('\n')
        hits = [i + 1 for i, l in enumerate(lines) if pat in l]
        if n is None:
            assert len(hits) == 1, (self.dir, f, pat, hits)
            return {'file': f, 'line': hits[0]}
        assert len(hits) >= n, (self.dir, f, pat, hits)
        return {'file': f, 'line': hits[n - 1]}

    def link(self, kind, frm, to, contract, client, server, evidence, in_scope=True,
             reason=None, hop='direct', label='high', **extra):
        d = {'kind': kind, 'from_service': frm, 'to_service': to, 'contract': contract,
             'client': client, 'server': server, 'evidence': evidence,
             'in_scope_static': in_scope}
        if not in_scope:
            assert reason
            d['out_of_scope_reason'] = reason
        d['hop'] = hop
        d['label_confidence'] = label
        d.update(extra)
        self.links.append(d)

    def dc(self, where, why):
        self.dont_care.append({**where, 'reason': why})

    def write(self):
        m = dict(self.meta)
        m['commit'] = head(self.dir)
        m['links'] = self.links
        m['dont_care_call_sites'] = self.dont_care
        for i, l in enumerate(m['links']):
            l['id'] = f"{m['corpus']}#{i+1}"
        s = {}
        for k in ('http', 'grpc', 'messaging'):
            s[k] = {'in_scope': sum(1 for l in self.links if l['kind'] == k and l['in_scope_static']),
                    'out_of_scope': sum(1 for l in self.links if l['kind'] == k and not l['in_scope_static'])}
        pairs = sorted({(l['from_service'], l['to_service'], l['kind']) for l in self.links if l['in_scope_static']})
        s['service_pairs_in_scope'] = [list(p) for p in pairs]
        m['summary'] = s
        order = ['corpus', 'repo', 'commit', 'license', 'languages', 'sources', 'services',
                 'summary', 'links', 'dont_care_call_sites', 'notes']
        m = {k: m[k] for k in order if k in m} | {k: v for k, v in m.items() if k not in order}
        with open(f"{OUT}/{m['corpus']}.json", 'w') as fh:
            json.dump(m, fh, indent=2)
            fh.write('\n')
        print(m['corpus'], m['commit'], json.dumps({k: v for k, v in s.items() if k != 'service_pairs_in_scope'}), len(pairs), 'pairs')


# ---------------------------------------------------------------- Online Boutique
ob = Corpus('microservices-demo',
    corpus='online-boutique',
    repo='https://github.com/GoogleCloudPlatform/microservices-demo',
    license='Apache-2.0',
    languages=['Go', 'C#', 'JavaScript (Node.js)', 'Python', 'Java'],
    sources=[
        'protos/demo.proto: authoritative service/rpc definitions (package hipstershop)',
        'README.md architecture table (which service talks to which) and kubernetes-manifests/*.yaml *_SERVICE_ADDR env vars: independent confirmation of every service pair',
        'manual reading of every client stub construction/call and every server implementation at the pinned commit',
    ],
    services=[
        {'name': 'frontend', 'root': 'src/frontend'},
        {'name': 'cartservice', 'root': 'src/cartservice'},
        {'name': 'productcatalogservice', 'root': 'src/productcatalogservice'},
        {'name': 'currencyservice', 'root': 'src/currencyservice'},
        {'name': 'paymentservice', 'root': 'src/paymentservice'},
        {'name': 'shippingservice', 'root': 'src/shippingservice'},
        {'name': 'emailservice', 'root': 'src/emailservice'},
        {'name': 'checkoutservice', 'root': 'src/checkoutservice'},
        {'name': 'recommendationservice', 'root': 'src/recommendationservice'},
        {'name': 'adservice', 'root': 'src/adservice'},
        {'name': 'loadgenerator', 'root': 'src/loadgenerator'},
        {'name': 'shoppingassistantservice', 'root': 'src/shoppingassistantservice'},
    ],
    notes=[
        'client.line is the line holding the RPC method name of the stub call (Go calls are chained across lines: pb.NewXClient(conn).\\n Method(...)). Recommend +-3 line tolerance.',
        'server.line is the line of the method implementing the rpc (Go/C#/Python/Java method def; Node: the handler function definition).',
        'contract_def points to the rpc line in protos/demo.proto; each language keeps its own copy of the proto (src/cartservice/src/protos/Cart.proto, src/adservice/src/main/proto/demo.proto, src/currencyservice/proto/demo.proto, src/paymentservice/proto/demo.proto) and Go/Python use generated code (genproto/, demo_pb2_grpc.py).',
        'hipstershop.ProductCatalogService/SearchProducts and the grpc.health.v1.Health/Check service are implemented but never called by another service: there is no link for them; a link to them is a false positive.',
        'emailservice registers DummyEmailService (email_server.py) by default; the real EmailService.SendOrderConfirmation is an alternative implementation of the same rpc. Either server line should count as correct.',
        'loadgenerator -> frontend: Locust HttpUser (requests-based) calls with literal paths; host comes from FRONTEND_ADDR at runtime but paths are literal, so they are in scope. frontend routes are prefixed with BASE_URL env (default "").',
        'Browser fetch() calls in src/frontend/templates/assistant.html target the frontend itself (/bot, /product-meta): intra-service, not labelled.',
        'shoppingassistantservice is an optional component (kustomize/components/shopping-assistant).',
    ])
PROTO = 'protos/demo.proto'
def rpc(svc, m):
    return ob.loc(PROTO, f'rpc {m}(')
FE_RPC = 'src/frontend/rpc.go'
CO = 'src/checkoutservice/main.go'
SRV = {
    ('CartService', 'AddItem'): ('cartservice', ob.loc('src/cartservice/src/services/CartService.cs', 'override Task<Empty> AddItem(')),
    ('CartService', 'GetCart'): ('cartservice', ob.loc('src/cartservice/src/services/CartService.cs', 'override Task<Cart> GetCart(')),
    ('CartService', 'EmptyCart'): ('cartservice', ob.loc('src/cartservice/src/services/CartService.cs', 'override Task<Empty> EmptyCart(')),
    ('ProductCatalogService', 'ListProducts'): ('productcatalogservice', ob.loc('src/productcatalogservice/product_catalog.go', ') ListProducts(')),
    ('ProductCatalogService', 'GetProduct'): ('productcatalogservice', ob.loc('src/productcatalogservice/product_catalog.go', ') GetProduct(')),
    ('CurrencyService', 'GetSupportedCurrencies'): ('currencyservice', ob.loc('src/currencyservice/server.js', 'function getSupportedCurrencies (')),
    ('CurrencyService', 'Convert'): ('currencyservice', ob.loc('src/currencyservice/server.js', 'function convert (')),
    ('PaymentService', 'Charge'): ('paymentservice', ob.loc('src/paymentservice/server.js', 'static ChargeServiceHandler(')),
    ('ShippingService', 'GetQuote'): ('shippingservice', ob.loc('src/shippingservice/main.go', ') GetQuote(')),
    ('ShippingService', 'ShipOrder'): ('shippingservice', ob.loc('src/shippingservice/main.go', ') ShipOrder(')),
    ('EmailService', 'SendOrderConfirmation'): ('emailservice', ob.loc('src/emailservice/email_server.py', 'def SendOrderConfirmation(', n=2)),
    ('CheckoutService', 'PlaceOrder'): ('checkoutservice', ob.loc(CO, ') PlaceOrder(')),
    ('RecommendationService', 'ListRecommendations'): ('recommendationservice', ob.loc('src/recommendationservice/recommendation_server.py', 'def ListRecommendations(')),
    ('AdService', 'GetAds'): ('adservice', ob.loc('src/adservice/src/main/java/hipstershop/AdService.java', 'public void getAds(')),
}
def grpc(frm, svc, m, client, ev):
    to, server = SRV[(svc, m)]
    extra = {}
    if (svc, m) == ('EmailService', 'SendOrderConfirmation'):
        extra['server_alternatives'] = [ob.loc('src/emailservice/email_server.py', 'def SendOrderConfirmation(', n=1)]
    ob.link('grpc', frm, to, f'hipstershop.{svc}/{m}', client, server, ev,
            contract_def=rpc(svc, m), **extra)

grpc('frontend', 'CurrencyService', 'GetSupportedCurrencies', ob.loc(FE_RPC, 'GetSupportedCurrencies(ctx'), 'pb.NewCurrencyServiceClient(fe.currencySvcConn) in getCurrencies')
grpc('frontend', 'ProductCatalogService', 'ListProducts', ob.loc(FE_RPC, 'ListProducts(ctx'), 'pb.NewProductCatalogServiceClient in getProducts')
grpc('frontend', 'ProductCatalogService', 'GetProduct', ob.loc(FE_RPC, 'GetProduct(ctx'), 'pb.NewProductCatalogServiceClient in getProduct')
grpc('frontend', 'CartService', 'GetCart', ob.loc(FE_RPC, '.GetCart(ctx'), 'pb.NewCartServiceClient in getCart')
grpc('frontend', 'CartService', 'EmptyCart', ob.loc(FE_RPC, '.EmptyCart(ctx'), 'pb.NewCartServiceClient in emptyCart')
grpc('frontend', 'CartService', 'AddItem', ob.loc(FE_RPC, '.AddItem(ctx'), 'pb.NewCartServiceClient in insertCart')
grpc('frontend', 'CurrencyService', 'Convert', ob.loc(FE_RPC, 'Convert(ctx'), 'pb.NewCurrencyServiceClient in convertCurrency')
grpc('frontend', 'ShippingService', 'GetQuote', ob.loc(FE_RPC, '.GetQuote(ctx'), 'pb.NewShippingServiceClient in getShippingQuote')
grpc('frontend', 'RecommendationService', 'ListRecommendations', ob.loc(FE_RPC, '.ListRecommendations(ctx'), 'pb.NewRecommendationServiceClient in getRecommendations')
grpc('frontend', 'AdService', 'GetAds', ob.loc(FE_RPC, '.GetAds(ctx'), 'pb.NewAdServiceClient in getAd')
grpc('frontend', 'CheckoutService', 'PlaceOrder', ob.loc('src/frontend/handlers.go', 'PlaceOrder(r.Context()'), 'pb.NewCheckoutServiceClient in placeOrderHandler')
grpc('checkoutservice', 'ShippingService', 'GetQuote', ob.loc(CO, 'GetQuote(ctx'), 'quoteShipping')
grpc('checkoutservice', 'CartService', 'GetCart', ob.loc(CO, '.GetCart(ctx'), 'getUserCart')
grpc('checkoutservice', 'CartService', 'EmptyCart', ob.loc(CO, '.EmptyCart(ctx'), 'emptyUserCart')
grpc('checkoutservice', 'ProductCatalogService', 'GetProduct', ob.loc(CO, 'cl.GetProduct(ctx'), 'prepOrderItems; stub held in variable cl')
grpc('checkoutservice', 'CurrencyService', 'Convert', ob.loc(CO, '.Convert(context.TODO()'), 'convertCurrency')
grpc('checkoutservice', 'PaymentService', 'Charge', ob.loc(CO, '.Charge(ctx'), 'chargeCard')
grpc('checkoutservice', 'EmailService', 'SendOrderConfirmation', ob.loc(CO, '.SendOrderConfirmation(ctx'), 'sendOrderConfirmation')
grpc('checkoutservice', 'ShippingService', 'ShipOrder', ob.loc(CO, '.ShipOrder(ctx'), 'shipOrder')
grpc('recommendationservice', 'ProductCatalogService', 'ListProducts', ob.loc('src/recommendationservice/recommendation_server.py', 'product_catalog_stub.ListProducts('), 'module-global demo_pb2_grpc.ProductCatalogServiceStub(channel) created in __main__')

FE_MAIN = 'src/frontend/main.go'
LG = 'src/loadgenerator/locustfile.py'
def lg(method, path, client, route_pat, ev='Locust HttpUser l.client (requests.Session); host from FRONTEND_ADDR'):
    ob.link('http', 'loadgenerator', 'frontend', f'{method} {path}', client, ob.loc(FE_MAIN, route_pat), ev)
lg('GET', '/', ob.loc(LG, 'l.client.get("/")'), 'baseUrl+"/", svc.homeHandler')
lg('POST', '/setCurrency', ob.loc(LG, 'l.client.post("/setCurrency"'), '"/setCurrency"')
lg('GET', '/product/{id}', ob.loc(LG, 'l.client.get("/product/" + random.choice'), '"/product/{id}"')
lg('GET', '/cart', ob.loc(LG, 'l.client.get("/cart")'), 'svc.viewCartHandler')
lg('GET', '/product/{id}', ob.loc(LG, 'l.client.get("/product/" + product)'), '"/product/{id}"')
lg('POST', '/cart', ob.loc(LG, 'l.client.post("/cart", {'), 'svc.addToCartHandler')
lg('POST', '/cart/empty', ob.loc(LG, "l.client.post('/cart/empty')"), '"/cart/empty"')
lg('POST', '/cart/checkout', ob.loc(LG, 'l.client.post("/cart/checkout"'), '"/cart/checkout"')
lg('GET', '/logout', ob.loc(LG, "l.client.get('/logout')"), '"/logout"')
ob.link('http', 'frontend', 'shoppingassistantservice', 'POST /',
        ob.loc('src/frontend/handlers.go', 'http.NewRequest(http.MethodPost, url, r.Body)'),
        ob.loc('src/shoppingassistantservice/shoppingassistantservice.py', '@app.route("/", methods=[\'POST\'])'),
        'chatBotHandler: url := "http://" + fe.shoppingAssistantSvcAddr; Flask route "/" POST',
        in_scope=False, reason='target host is only the env var SHOPPING_ASSISTANT_SERVICE_ADDR (set in kubernetes manifests); URL path is just the host root')
for f, pat, why in [
    ('src/currencyservice/client.js', 'client.getSupportedCurrencies(', 'developer test client inside currencyservice; not deployed, would be a self-link'),
    ('src/currencyservice/client.js', 'client.convert(', 'developer test client inside currencyservice'),
    ('src/recommendationservice/client.py', 'stub.ListRecommendations(request)', 'developer test client inside recommendationservice'),
]:
    ob.dc(ob.loc(f, pat), why)
ob.dc({'file': 'src/adservice/src/main/java/hipstershop/AdServiceClient.java', 'line': None}, 'developer test client inside adservice (any line)')
ob.dc({'file': 'src/emailservice/email_client.py', 'line': None}, 'developer test client inside emailservice (any line)')
ob.dc({'file': 'src/frontend/templates/assistant.html', 'line': None}, 'browser fetch() to the frontend itself')
ob.write()

# ---------------------------------------------------------------- robot-shop
rs = Corpus('robot-shop',
    corpus='robot-shop',
    repo='https://github.com/instana/robot-shop',
    license='Apache-2.0',
    languages=['JavaScript (Node.js/Express)', 'JavaScript (AngularJS browser)', 'Python (Flask, requests, pika, Locust)', 'Java (Spring Boot, Apache HttpClient)', 'Go (amqp)', 'PHP (Symfony, curl)', 'nginx config'],
    sources=[
        'README.md service list and docker-compose.yaml (services and dependencies)',
        'web/default.conf.template + web/Dockerfile ENV (*_HOST defaults) for the /api/<svc>/ proxy prefix mapping',
        'manual reading of every outbound HTTP/AMQP call and every route/consumer at the pinned commit',
    ],
    services=[
        {'name': 'web', 'root': 'web'}, {'name': 'cart', 'root': 'cart'}, {'name': 'catalogue', 'root': 'catalogue'},
        {'name': 'user', 'root': 'user'}, {'name': 'payment', 'root': 'payment'}, {'name': 'shipping', 'root': 'shipping'},
        {'name': 'ratings', 'root': 'ratings'}, {'name': 'dispatch', 'root': 'dispatch'}, {'name': 'load-gen', 'root': 'load-gen'},
    ],
    notes=[
        'hop=direct: service code calls another service. hop=frontend_via_proxy: AngularJS (web/static/js/controller.js) or Locust (load-gen/robot-shop.py) calls /api/<svc>/<path>; nginx (web/default.conf.template) strips /api/<svc>/ and proxies to http://${<SVC>_HOST}:8080/ whose defaults (catalogue, user, cart, shipping, payment, ratings) are set by ENV in web/Dockerfile. The backend path is the URL with /api/<svc> removed. from_service for these is the caller (web or load-gen), to_service the backend.',
        'hop=gateway_route: the nginx location block itself (one per backend); prefix-level, no single handler, so out of scope for call-site scoring.',
        'Host names in direct calls come from env vars with literal defaults equal to the service names (CATALOGUE_HOST||"catalogue", USER_HOST "user", CART_HOST "cart", CART_ENDPOINT "cart", CATALOGUE_URL ?: "http://catalogue:8080").',
        'shipping -> cart: URL is assembled across classes (Controller.CART_URL field passed to new CartHelper(url), then HttpPost(baseUrl + id)); needs inter-procedural constant propagation.',
        'For clients whose URL is in a variable assigned a few lines earlier (controller.js addToCart/rateProduct/change), client.line is the $http({ line; the URL literal is 1-3 lines above.',
        'payment -> external PAYMENT_GATEWAY (https://paypal.com/) is an external call, not labelled.',
        'Messaging: payment publishes with pika basic_publish(exchange="robot-shop" (direct), routing_key="orders"); dispatch declares queue "orders", binds it to exchange robot-shop with key "orders" and consumes from "orders".',
        'ratings is PHP; if the extractor does not support PHP, score the ratings->catalogue link and links whose server is ratings as unsupported-language rather than misses.',
    ])
W = 'web/static/js/controller.js'
LGR = 'load-gen/robot-shop.py'
CAT = 'catalogue/server.js'; CART = 'cart/server.js'; USR = 'user/server.js'
SHIP = 'shipping/src/main/java/com/instana/robotshop/shipping/Controller.java'
RAT = 'ratings/html/src/Controller/RatingsApiController.php'
PAY = 'payment/payment.py'
S = {
    'cat_products_cat': ('catalogue', rs.loc(CAT, "app.get('/products/:cat'")),
    'cat_products': ('catalogue', rs.loc(CAT, "app.get('/products',")),
    'cat_categories': ('catalogue', rs.loc(CAT, "app.get('/categories'")),
    'cat_search': ('catalogue', rs.loc(CAT, "app.get('/search/:text'")),
    'cat_product': ('catalogue', rs.loc(CAT, "app.get('/product/:sku'")),
    'user_uniqueid': ('user', rs.loc(USR, "app.get('/uniqueid'")),
    'user_check': ('user', rs.loc(USR, "app.get('/check/:id'")),
    'user_login': ('user', rs.loc(USR, "app.post('/login'")),
    'user_register': ('user', rs.loc(USR, "app.post('/register'")),
    'user_order': ('user', rs.loc(USR, "app.post('/order/:id'")),
    'user_history': ('user', rs.loc(USR, "app.get('/history/:id'")),
    'cart_get': ('cart', rs.loc(CART, "app.get('/cart/:id'")),
    'cart_delete': ('cart', rs.loc(CART, "app.delete('/cart/:id'")),
    'cart_rename': ('cart', rs.loc(CART, "app.get('/rename/:from/:to'")),
    'cart_add': ('cart', rs.loc(CART, "app.get('/add/:id/:sku/:qty'")),
    'cart_update': ('cart', rs.loc(CART, "app.get('/update/:id/:sku/:qty'")),
    'cart_shipping': ('cart', rs.loc(CART, "app.post('/shipping/:id'")),
    'ship_codes': ('shipping', rs.loc(SHIP, '@GetMapping("/codes")')),
    'ship_cities': ('shipping', rs.loc(SHIP, '@GetMapping("/cities/{code}")')),
    'ship_match': ('shipping', rs.loc(SHIP, '@GetMapping("/match/{code}/{text}")')),
    'ship_calc': ('shipping', rs.loc(SHIP, '@GetMapping("/calc/{id}")')),
    'ship_confirm': ('shipping', rs.loc(SHIP, '@PostMapping(path = "/confirm/{id}"')),
    'pay_pay': ('payment', rs.loc(PAY, "@app.route('/pay/<id>', methods=['POST'])")),
    'rat_rate': ('ratings', rs.loc(RAT, '@Route(path="/rate/{sku}/{score}", methods={"PUT"})')),
    'rat_fetch': ('ratings', rs.loc(RAT, '@Route("/fetch/{sku}", methods={"GET"})')),
}
def r_direct(frm, key, contract, client, ev, **kw):
    to, srv = S[key]
    rs.link('http', frm, to, contract, client, srv, ev, **kw)
r_direct('cart', 'cat_product', 'GET /product/{sku}', rs.loc(CART, "request('http://' + catalogueHost + ':8080/product/' + sku"),
         "request() (npm 'request'); catalogueHost = process.env.CATALOGUE_HOST || 'catalogue'")
r_direct('payment', 'user_check', 'GET /check/{id}', rs.loc(PAY, "requests.get('http://{user}:8080/check/{id}'"), 'requests.get; USER = os.getenv("USER_HOST", "user")')
r_direct('payment', 'user_order', 'POST /order/{id}', rs.loc(PAY, "requests.post('http://{user}:8080/order/{id}'"), 'requests.post')
r_direct('payment', 'cart_delete', 'DELETE /cart/{id}', rs.loc(PAY, "requests.delete('http://{cart}:8080/cart/{id}'"), 'requests.delete; CART = os.getenv("CART_HOST", "cart")')
r_direct('shipping', 'cart_shipping', 'POST /shipping/{id}', rs.loc('shipping/src/main/java/com/instana/robotshop/shipping/CartHelper.java', 'new HttpPost(baseUrl + id)'),
         'Apache HttpClient; baseUrl = CART_URL = String.format("http://%s/shipping/", getenv("CART_ENDPOINT", "cart")) (Controller.java), passed via new CartHelper(CART_URL)',
         client_url_def=rs.loc(SHIP, 'private String CART_URL'))
r_direct('ratings', 'cat_product', 'GET /product/{sku}', rs.loc('ratings/html/src/Service/CatalogueService.php', '$curl = curl_init($url);'),
         "PHP curl; $url = sprintf('%s/product/%s', catalogueUrl, sku); catalogueUrl = getenv('CATALOGUE_URL') ?: 'http://catalogue:8080' (Kernel.php)",
         client_url_def=rs.loc('ratings/html/src/Service/CatalogueService.php', "sprintf('%s/product/%s'"))
rs.link('messaging', 'payment', 'dispatch', 'amqp:exchange=robot-shop;routing_key=orders;queue=orders',
        rs.loc('payment/rabbitmq.py', 'self._channel.basic_publish(exchange=self.EXCHANGE'),
        rs.loc('dispatch/main.go', 'rabbitChan.Consume("orders"'),
        'pika basic_publish with class constants EXCHANGE="robot-shop", ROUTING_KEY="orders"; dispatch QueueBind("orders","orders","robot-shop") then Consume("orders")',
        producer_invocation=rs.loc(PAY, 'publisher.publish(order, headers)'),
        consumer_binding=rs.loc('dispatch/main.go', 'rabbitChan.QueueBind(queue.Name, "orders", "robot-shop"'))

def via(frm, key, method, backend_path, client, ev):
    to, srv = S[key]
    rs.link('http', frm, to, f'{method} {backend_path}', client, srv, ev, hop='frontend_via_proxy')
NG = 'nginx /api/{0}/ -> http://${{{1}_HOST}}:8080/ (web/default.conf.template)'
web_calls = [
    (83, 'cat_products_cat', 'GET', '/products/{cat}', "'/api/catalogue/products/' + category"),
    (102, 'cat_categories', 'GET', '/categories', "'/api/catalogue/categories'"),
    (116, 'user_uniqueid', 'GET', '/uniqueid', "'/api/user/uniqueid'"),
    (175, 'cat_search', 'GET', '/search/{text}', "'/api/catalogue/search/' + text"),
    (203, 'cart_add', 'GET', '/add/{id}/{sku}/{qty}', "var url = '/api/cart/add/'"),
    (221, 'rat_rate', 'PUT', '/api/rate/{sku}/{score}', "var url = '/api/ratings/api/rate/'"),
    (243, 'cat_product', 'GET', '/product/{sku}', "'/api/catalogue/product/' + sku"),
    (254, 'rat_fetch', 'GET', '/api/fetch/{sku}', "'/api/ratings/api/fetch/' + sku"),
    (287, 'cart_update', 'GET', '/update/{id}/{sku}/{qty}', "var url = '/api/cart/update/' + $scope.data.uniqueid"),
    (299, 'cart_get', 'GET', '/cart/{id}', "'/api/cart/cart/' + id"),
    (306, 'cart_update', 'GET', '/update/{id}/{sku}/{qty}', "'/api/cart/update/' + id + '/SHIP/0'"),
    (338, 'ship_calc', 'GET', '/calc/{id}', "'/api/shipping/calc/' + uuid"),
    (352, 'ship_confirm', 'POST', '/confirm/{id}', "'/api/shipping/confirm/' + currentUser.uniqueid"),
    (382, 'ship_codes', 'GET', '/codes', "'/api/shipping/codes'"),
    (398, 'ship_match', 'GET', '/match/{code}/{text}', "'/api/shipping/match/'"),
    (439, 'pay_pay', 'POST', '/pay/{id}', "'/api/payment/pay/' + $scope.data.uniqueid"),
    (474, 'user_login', 'POST', '/login', "'/api/user/login'"),
    (489, 'cart_rename', 'GET', '/rename/{from}/{to}', "'/api/cart/rename/' + oldId"),
    (520, 'user_register', 'POST', '/register', "'/api/user/register'"),
    (544, 'user_history', 'GET', '/history/{id}', "'/api/user/history/' + id"),
]
src_lines = open(f'{C}/robot-shop/{W}').read().split('\n')
for line, key, m, p, urlpat in web_calls:
    assert '$http({' in src_lines[line - 1], line
    # url literal must be within 4 lines above/below
    near = '\n'.join(src_lines[line - 5:line + 3])
    assert urlpat in near, (line, urlpat)
    svc = S[key][0]
    via('web', key, m, p, {'file': W, 'line': line}, 'AngularJS $http({url: ' + urlpat.replace("var url = ", "") + '...}); ' + NG.format(svc, svc.upper()))
lg_calls = [
    ("self.client.post('/api/user/login'", 'user_login', 'POST', '/login'),
    ("self.client.get('/api/user/uniqueid'", 'user_uniqueid', 'GET', '/uniqueid'),
    ("self.client.get('/api/catalogue/categories'", 'cat_categories', 'GET', '/categories'),
    ("self.client.get('/api/catalogue/products'", 'cat_products', 'GET', '/products'),
    ("self.client.put('/api/ratings/api/rate/", 'rat_rate', 'PUT', '/api/rate/{sku}/{score}'),
    ("self.client.get('/api/catalogue/product/", 'cat_product', 'GET', '/product/{sku}'),
    ("self.client.get('/api/ratings/api/fetch/", 'rat_fetch', 'GET', '/api/fetch/{sku}'),
    ("self.client.get('/api/cart/add/", 'cart_add', 'GET', '/add/{id}/{sku}/{qty}'),
    ("self.client.get('/api/cart/cart/", 'cart_get', 'GET', '/cart/{id}'),
    ("self.client.get('/api/cart/update/", 'cart_update', 'GET', '/update/{id}/{sku}/{qty}'),
    ("self.client.get('/api/shipping/codes'", 'ship_codes', 'GET', '/codes'),
    ("self.client.get('/api/shipping/cities/", 'ship_cities', 'GET', '/cities/{code}'),
    ("self.client.get('/api/shipping/calc/", 'ship_calc', 'GET', '/calc/{id}'),
    ("self.client.post('/api/shipping/confirm/", 'ship_confirm', 'POST', '/confirm/{id}'),
    ("self.client.post('/api/payment/pay/{}'", 'pay_pay', 'POST', '/pay/{id}'),
    ("self.client.post('/api/payment/pay/partner-57'", 'pay_pay', 'POST', '/pay/{id}'),
]
for pat, key, m, p in lg_calls:
    svc = S[key][0]
    via('load-gen', key, m, p, rs.loc(LGR, pat), 'Locust HttpUser self.client to web host; ' + NG.format(svc, svc.upper()))
rs.dc(rs.loc(LGR, "self.client.get('/', headers"), 'GET / served by nginx static files of web itself')
rs.dc(rs.loc(PAY, 'requests.get(PAYMENT_GATEWAY)'), 'external call to https://paypal.com/')
TPL = 'web/default.conf.template'
for svc, port in [('catalogue', 8080), ('user', 8080), ('cart', 8080), ('shipping', 8080), ('payment', 8080), ('ratings', 80)]:
    rs.link('http', 'web', svc, f'ANY /api/{svc}/** -> /**', rs.loc(TPL, f'location /api/{svc}/ {{'), None,
            f'nginx proxy_pass http://${{{svc.upper()}_HOST}}:{port}/', in_scope=False,
            reason='declarative reverse-proxy prefix route (config, not a code call site); no single handler', hop='gateway_route')
rs.write()

# ---------------------------------------------------------------- microSecEnD: ewolff/microservice-kafka
MS = 'https://github.com/tuhh-softsec/microSecEnD (MSR 2023, Schneider et al.; dataset/{app}/{app}.json, *_traceability.json, *.py)'
ek = Corpus('ewolff_microservice-kafka',
    corpus='msecend-ewolff-microservice-kafka',
    repo='https://github.com/ewolff/microservice-kafka',
    license='Apache-2.0',
    languages=['Java (Spring Boot, Spring Kafka)', 'Apache httpd config'],
    sources=[
        MS.format(app='ewolff_microservice-kafka') + ': commit 6fd9cc6 taken from the artifact URLs in ewolff_microservice-kafka.py; DFD flows order->kafka (Producer Topic order), kafka->invoicing and kafka->shipping (Consumer Topic order), apache->order/shipping/invoicing',
        'manual reading of all Java sources and docker/apache/000-default.conf at the pinned commit (labels re-derived from source, DFD used only for service-pair cross-check)',
    ],
    services=[
        {'name': 'order', 'root': 'microservice-kafka/microservice-kafka-order', 'dfd_name': 'order'},
        {'name': 'invoicing', 'root': 'microservice-kafka/microservice-kafka-invoicing', 'dfd_name': 'invoicing'},
        {'name': 'shipping', 'root': 'microservice-kafka/microservice-kafka-shipping', 'dfd_name': 'shipping'},
        {'name': 'apache', 'root': 'docker/apache', 'dfd_name': 'apache'},
    ],
    notes=[
        'microSecEnD models brokers as nodes (order -> kafka -> invoicing); converted here to producer -> consumer per topic.',
        'There is no service-to-service HTTP call in code; Apache ProxyPass routes are recorded as gateway_route links (out of scope for call-site scoring).',
        'Label disagreements with microSecEnD: (1) the Kafka flows are stereotyped restful_http in addition to message_producer/consumer_kafka; (2) traceability URLs for the service nodes and postgres edges contain a doubled path segment (blob/master/master/...) and the kafka->invoicing/shipping edges give a bare file name, so 11 of 27 file references do not resolve; (3) apache->* edges point at line 13/16/19 of 000-default.conf, one line above the ProxyPass lines (14/17/20); (4) the order->kafka edge points at the method declaration (OrderService.java:33) rather than the send (line 34). Service pairs and topic agree with source.',
        'Test-only Kafka listeners (src/test/.../KafkaListenerBean.java) are not labelled.',
    ])
ORD = 'microservice-kafka/microservice-kafka-order/src/main/java/com/ewolff/microservice/order/logic/OrderService.java'
for svc in ('invoicing', 'shipping'):
    ek.link('messaging', 'order', svc, 'kafka-topic:order',
            ek.loc(ORD, 'kafkaTemplate.send("order"'),
            ek.loc(f'microservice-kafka/microservice-kafka-{svc}/src/main/java/com/ewolff/microservice/{svc}/events/OrderKafkaListener.java', '@KafkaListener(topics = "order")'),
            f'KafkaTemplate.send("order", key, order) -> @KafkaListener(topics = "order") consumer group {svc}')
ek.dc({'file': 'microservice-kafka/microservice-kafka-order/src/test/java/com/ewolff/microservice/order/kafka/KafkaListenerBean.java', 'line': None}, 'test-only listener')
for svc in ('order', 'shipping', 'invoicing'):
    ek.link('http', 'apache', svc, f'ANY /{svc}/** -> /**', ek.loc('docker/apache/000-default.conf', f'ProxyPass        /{svc} '), None,
            f'Apache ProxyPass /{svc} http://{svc}:8080/', in_scope=False,
            reason='declarative reverse-proxy prefix route (config, not a code call site)', hop='gateway_route')
ek.write()

# ---------------------------------------------------------------- microSecEnD: sqshq/piggymetrics
pm = Corpus('sqshq_piggymetrics',
    corpus='msecend-piggymetrics',
    repo='https://github.com/sqshq/piggymetrics',
    license='MIT',
    languages=['Java (Spring Boot, Spring Cloud Netflix: Feign, Zuul, Hystrix, Turbine Stream; Spring Cloud Bus AMQP)', 'YAML config'],
    sources=[
        MS.format(app='sqshq_piggymetrics') + ': commit fd5ee3c from the artifact URLs in sqshq_piggymetrics.py; flows with feign_connection stereotype, gateway->* flows, auth_service->* flows, *->rabbitmq->turbine_stream_service flows',
        'manual reading of Feign clients, controllers and config/src/main/resources/shared/*.yml (server.servlet.context-path per service) at the pinned commit',
    ],
    services=[
        {'name': 'account-service', 'root': 'account-service', 'dfd_name': 'account_service'},
        {'name': 'statistics-service', 'root': 'statistics-service', 'dfd_name': 'statistics_service'},
        {'name': 'notification-service', 'root': 'notification-service', 'dfd_name': 'notification_service'},
        {'name': 'auth-service', 'root': 'auth-service', 'dfd_name': 'auth_service'},
        {'name': 'gateway', 'root': 'gateway', 'dfd_name': 'gateway'},
        {'name': 'turbine-stream-service', 'root': 'turbine-stream-service', 'dfd_name': 'turbine_stream_service'},
        {'name': 'config', 'root': 'config', 'dfd_name': 'config'},
        {'name': 'registry', 'root': 'registry', 'dfd_name': 'registry'},
        {'name': 'monitoring', 'root': 'monitoring', 'dfd_name': 'monitoring'},
    ],
    notes=[
        'Feign paths are matched to server handlers including the server context-path from config/src/main/resources/shared/<svc>.yml: account-service /accounts, statistics-service /statistics, notification-service /notifications, auth-service /uaa. Without context-path the Feign path (/statistics/{accountName}) does not equal the controller path (/{accountName}).',
        'client.line is the Feign method @RequestMapping line; client_invocations lists where the Feign method is called.',
        'Infrastructure flows of the DFD (config server, Eureka registry, MongoDB, SMTP) are not contract links and are omitted.',
        'Out of scope (framework/config driven, no call site): Spring Security OAuth2 user-info-uri and client-credentials token calls to auth-service (DFD models them as auth_service -> X), Hystrix metrics over RabbitMQ to turbine-stream-service (spring-cloud-netflix-hystrix-stream), Spring Cloud Bus (springCloudBus exchange) between account/statistics/notification, Zuul routes.',
        'statistics-service -> https://api.exchangeratesapi.io (Feign url=${rates.url}) is external; not a link.',
        'gateway/src/main/resources/static/js/*.js ($.ajax via Zuul to accounts/, statistics/, notifications/, uaa/) is NOT labelled (login.js is a single CR-delimited line); treat any link from gateway static JS as dont-care.',
        'Label disagreements with microSecEnD: (1) auth_service -> account/statistics/notification are traced to accessTokenUri lines; in code the HTTP caller is the resource service (direction is data-flow, not call); (2) account/statistics/notification -> rabbitmq are all traced to an import line in turbine-stream-service (TurbineStreamServiceApplication.java:6) instead of the producing service (its pom hystrix-stream dependency); (3) 2 traceability paths contain a stray " 2" (e.g. "pom 2.xml") and do not resolve. Feign pairs agree with source.',
    ])
AC = 'account-service/src/main/java/com/piggymetrics/account'
pm.link('http', 'account-service', 'auth-service', 'POST /uaa/users',
        pm.loc(f'{AC}/client/AuthServiceClient.java', 'value = "/uaa/users"'),
        pm.loc('auth-service/src/main/java/com/piggymetrics/auth/controller/UserController.java', '@RequestMapping(method = RequestMethod.POST)'),
        '@FeignClient(name = "auth-service"); UserController @RequestMapping("/users") POST; auth context-path /uaa',
        client_invocations=[pm.loc(f'{AC}/service/AccountServiceImpl.java', 'authClient.createUser(user)')])
pm.link('http', 'account-service', 'statistics-service', 'PUT /statistics/{accountName}',
        pm.loc(f'{AC}/client/StatisticsServiceClient.java', 'value = "/statistics/{accountName}"'),
        pm.loc('statistics-service/src/main/java/com/piggymetrics/statistics/controller/StatisticsController.java', 'value = "/{accountName}", method = RequestMethod.PUT'),
        '@FeignClient(name = "statistics-service", fallback=...); StatisticsController PUT /{accountName}; context-path /statistics',
        client_invocations=[pm.loc(f'{AC}/service/AccountServiceImpl.java', 'statisticsClient.updateStatistics(name, account)')])
pm.link('http', 'notification-service', 'account-service', 'GET /accounts/{accountName}',
        pm.loc('notification-service/src/main/java/com/piggymetrics/notification/client/AccountServiceClient.java', 'value = "/accounts/{accountName}"'),
        pm.loc(f'{AC}/controller/AccountController.java', 'path = "/{name}", method = RequestMethod.GET'),
        '@FeignClient(name = "account-service"); AccountController GET /{name}; context-path /accounts',
        client_invocations=[pm.loc('notification-service/src/main/java/com/piggymetrics/notification/service/NotificationServiceImpl.java', 'client.getAccount(recipient.getAccountName())')])
UIU = pm.loc('config/src/main/resources/shared/application.yml', 'user-info-uri: http://auth-service:5000/uaa/users/current')
for svc in ('account-service', 'statistics-service', 'notification-service'):
    pm.link('http', svc, 'auth-service', 'GET /uaa/users/current', UIU,
            pm.loc('auth-service/src/main/java/com/piggymetrics/auth/controller/UserController.java', 'value = "/current", method = RequestMethod.GET'),
            'Spring Security OAuth2 resource server validates tokens via security.oauth2.resource.user-info-uri (shared application.yml)',
            in_scope=False, reason='framework call configured only by a URL in YAML; no call site in code')
    pm.link('http', svc, 'auth-service', 'POST /uaa/oauth/token',
            pm.loc(f'config/src/main/resources/shared/{svc}.yml', 'accessTokenUri: http://auth-service:5000/uaa/oauth/token'), None,
            'OAuth2RestTemplate client_credentials grant; endpoint is provided by Spring Security OAuth2 framework in auth-service, no handler in source',
            in_scope=False, reason='framework call configured only in YAML; server endpoint is framework-provided')
    pm.link('messaging', svc, 'turbine-stream-service', 'rabbit:springCloudHystrixStream (framework default)',
            pm.loc(f'{svc}/pom.xml', '<artifactId>spring-cloud-netflix-hystrix-stream</artifactId>'),
            pm.loc('turbine-stream-service/pom.xml', '<artifactId>spring-cloud-starter-netflix-turbine-stream</artifactId>'),
            'Hystrix metrics published to RabbitMQ by hystrix-stream starter; consumed by @EnableTurbineStream',
            in_scope=False, reason='implicit: produced/consumed by framework starters on a default destination; only evidence is a Maven dependency')
for svc, path, pat in [('auth-service', '/uaa/**', 'path: /uaa/**'), ('account-service', '/accounts/**', 'path: /accounts/**'),
                       ('statistics-service', '/statistics/**', 'path: /statistics/**'), ('notification-service', '/notifications/**', 'path: /notifications/**')]:
    pm.link('http', 'gateway', svc, f'ANY {path}', pm.loc('config/src/main/resources/shared/gateway.yml', pat), None,
            'Zuul route (stripPrefix: false)', in_scope=False, reason='declarative gateway prefix route in YAML; no single handler', hop='gateway_route')
pm.dc(pm.loc('statistics-service/src/main/java/com/piggymetrics/statistics/client/ExchangeRatesClient.java', '@FeignClient(url = "${rates.url}"'), 'external API (api.exchangeratesapi.io)')
pm.dc({'file': 'gateway/src/main/resources/static/js', 'line': None}, 'browser JS via Zuul; not labelled')
pm.write()

# ---------------------------------------------------------------- microSecEnD: spring-petclinic-microservices
pc = Corpus('spring-petclinic-microservices',
    corpus='msecend-spring-petclinic-microservices',
    repo='https://github.com/spring-petclinic/spring-petclinic-microservices',
    license='Apache-2.0',
    languages=['Java (Spring Boot, Spring Cloud Gateway, WebClient)', 'JavaScript (AngularJS browser)', 'YAML config'],
    sources=[
        MS.format(app='spring-petclinic_spring-petclinic-microservices') + ': commit 0ab2e95 from the artifact URLs in the .py model (one reference uses c47e99a); flows api_gateway -> customers/visits/vets_service (load_balanced_link)',
        'manual reading of api-gateway WebClient clients, Spring Cloud Gateway routes (api-gateway application.yml), AngularJS controllers under api-gateway static/scripts, and the three service controllers at the pinned commit',
    ],
    services=[
        {'name': 'api-gateway', 'root': 'spring-petclinic-api-gateway', 'dfd_name': 'api_gateway'},
        {'name': 'customers-service', 'root': 'spring-petclinic-customers-service', 'dfd_name': 'customers_service'},
        {'name': 'visits-service', 'root': 'spring-petclinic-visits-service', 'dfd_name': 'visits_service'},
        {'name': 'vets-service', 'root': 'spring-petclinic-vets-service', 'dfd_name': 'vets_service'},
        {'name': 'config-server', 'root': 'spring-petclinic-config-server', 'dfd_name': 'config_server'},
        {'name': 'discovery-server', 'root': 'spring-petclinic-discovery-server', 'dfd_name': 'discovery_server'},
        {'name': 'admin-server', 'root': 'spring-petclinic-admin-server', 'dfd_name': 'admin_server'},
    ],
    notes=[
        'hop=direct: api-gateway Java WebClient calls with lb-resolved host names (http://customers-service/..., http://visits-service/...).',
        'hop=frontend_via_gateway: AngularJS UI served by api-gateway calls api/<x>/...; Spring Cloud Gateway routes /api/vet/**, /api/visit/**, /api/customer/** to lb://<svc> with StripPrefix=2, so backend path = URL minus the first two segments. These are static (route + literal) but need route rewriting.',
        'Service configuration (ports etc.) lives in the external repo spring-petclinic-microservices-config; it was not needed for paths (no context-path in this repo).',
        'Class-level @RequestMapping is combined with method mappings: OwnerResource /owners, VetResource /vets; PetResource and VisitResource have no class prefix and use patterns with * (owners/*/pets/{petId}).',
        'owner-details.controller.js calls api/gateway/owners/{id}, which is api-gateway\'s own ApiGatewayController (intra-service, not a link).',
        'Label disagreements with microSecEnD: (1) the three api_gateway -> service edges are all traced to the same line (application.yml:9, the vets route); (2) the DFD/traceability ignore the code-level WebClient clients (CustomersServiceClient, VisitsServiceClient), which are the only direct service-to-service calls; (3) 15 traceability references point to files in the external config repo by bare file name. The service pairs agree with source.',
    ])
GW = 'spring-petclinic-api-gateway/src/main/java/org/springframework/samples/petclinic/api/application'
CUS = 'spring-petclinic-customers-service/src/main/java/org/springframework/samples/petclinic/customers/web'
VIS = 'spring-petclinic-visits-service/src/main/java/org/springframework/samples/petclinic/visits/web/VisitResource.java'
VET = 'spring-petclinic-vets-service/src/main/java/org/springframework/samples/petclinic/vets/web/VetResource.java'
P = {
    'owner_get': ('customers-service', pc.loc(f'{CUS}/OwnerResource.java', '@GetMapping(value = "/{ownerId}")')),
    'owner_list': ('customers-service', pc.loc(f'{CUS}/OwnerResource.java', '    @GetMapping', n=2)),
    'owner_create': ('customers-service', pc.loc(f'{CUS}/OwnerResource.java', '@PostMapping')),
    'owner_update': ('customers-service', pc.loc(f'{CUS}/OwnerResource.java', '@PutMapping(value = "/{ownerId}")')),
    'pet_types': ('customers-service', pc.loc(f'{CUS}/PetResource.java', '@GetMapping("/petTypes")')),
    'pet_create': ('customers-service', pc.loc(f'{CUS}/PetResource.java', '@PostMapping("/owners/{ownerId}/pets")')),
    'pet_update': ('customers-service', pc.loc(f'{CUS}/PetResource.java', '@PutMapping("/owners/*/pets/{petId}")')),
    'pet_get': ('customers-service', pc.loc(f'{CUS}/PetResource.java', '@GetMapping("owners/*/pets/{petId}")')),
    'visits_post': ('visits-service', pc.loc(VIS, '@PostMapping("owners/*/pets/{petId}/visits")')),
    'visits_get': ('visits-service', pc.loc(VIS, '@GetMapping("owners/*/pets/{petId}/visits")')),
    'visits_pets': ('visits-service', pc.loc(VIS, '@GetMapping("pets/visits")')),
    'vets_list': ('vets-service', pc.loc(VET, '@GetMapping')),
}
assert '@GetMapping' in open(f'{C}/spring-petclinic-microservices/{CUS}/OwnerResource.java').read().split('\n')[P['owner_list'][1]['line'] - 1]
to, srv = P['owner_get']
pc.link('http', 'api-gateway', to, 'GET /owners/{ownerId}', pc.loc(f'{GW}/CustomersServiceClient.java', '.uri("http://customers-service/owners/{ownerId}"'), srv,
        'WebClient (load-balanced builder) .get().uri("http://customers-service/owners/{ownerId}")',
        client_invocations=[pc.loc('spring-petclinic-api-gateway/src/main/java/org/springframework/samples/petclinic/api/boundary/web/ApiGatewayController.java', 'customersServiceClient.getOwner(ownerId)')])
to, srv = P['visits_pets']
pc.link('http', 'api-gateway', to, 'GET /pets/visits?petId={ids}', pc.loc(f'{GW}/VisitsServiceClient.java', '.uri(hostname + "pets/visits?petId={petId}"'), srv,
        'WebClient .get().uri(hostname + "pets/visits?petId={petId}") with field hostname = "http://visits-service/" (setter exists for tests)',
        client_url_def=pc.loc(f'{GW}/VisitsServiceClient.java', 'private String hostname = "http://visits-service/"'),
        client_invocations=[pc.loc('spring-petclinic-api-gateway/src/main/java/org/springframework/samples/petclinic/api/boundary/web/ApiGatewayController.java', 'visitsServiceClient.getVisitsForPets(owner.getPetIds())')])
UI = 'spring-petclinic-api-gateway/src/main/resources/static/scripts'
ROUTE = {'customers-service': 'Path=/api/customer/**', 'visits-service': 'Path=/api/visit/**', 'vets-service': 'Path=/api/vet/**'}
for f, pat, key, m, path in [
    ('vet-list/vet-list.controller.js', "$http.get('api/vet/vets')", 'vets_list', 'GET', '/vets'),
    ('owner-form/owner-form.controller.js', '$http.get("api/customer/owners/" + ownerId)', 'owner_get', 'GET', '/owners/{ownerId}'),
    ('owner-form/owner-form.controller.js', '$http.put("api/customer/owners/" + id', 'owner_update', 'PUT', '/owners/{ownerId}'),
    ('owner-form/owner-form.controller.js', '$http.post("api/customer/owners"', 'owner_create', 'POST', '/owners'),
    ('owner-list/owner-list.controller.js', "$http.get('api/customer/owners')", 'owner_list', 'GET', '/owners'),
    ('pet-form/pet-form.controller.js', "$http.get('api/customer/petTypes')", 'pet_types', 'GET', '/petTypes'),
    ('pet-form/pet-form.controller.js', '$http.get("api/customer/owners/" + ownerId + "/pets/" + petId)', 'pet_get', 'GET', '/owners/{ownerId}/pets/{petId}'),
    ('pet-form/pet-form.controller.js', "$http.get('api/customer/owners/' + ownerId)", 'owner_get', 'GET', '/owners/{ownerId}'),
    ('pet-form/pet-form.controller.js', '$http.put("api/customer/owners/" + ownerId + "/pets/" + id', 'pet_update', 'PUT', '/owners/{ownerId}/pets/{petId}'),
    ('pet-form/pet-form.controller.js', '$http.post("api/customer/owners/" + ownerId + "/pets"', 'pet_create', 'POST', '/owners/{ownerId}/pets'),
    ('visits/visits.controller.js', '$http.get(url)', 'visits_get', 'GET', '/owners/{ownerId}/pets/{petId}/visits'),
    ('visits/visits.controller.js', '$http.post(url, data)', 'visits_post', 'POST', '/owners/{ownerId}/pets/{petId}/visits'),
]:
    to, srv = P[key]
    ev = f'AngularJS $http in UI served by api-gateway; Spring Cloud Gateway {ROUTE[to]} -> lb://{to}, StripPrefix=2'
    extra = {}
    if 'visits' in f:
        extra['client_url_def'] = pc.loc(f'{UI}/{f}', 'var url = "api/visit/owners/"')
    pc.link('http', 'api-gateway', to, f'{m} {path}', pc.loc(f'{UI}/{f}', pat), srv, ev, hop='frontend_via_gateway', **extra)
pc.dc(pc.loc(f'{UI}/owner-details/owner-details.controller.js', "$http.get('api/gateway/owners/'"), 'intra-service: api-gateway own controller')
GWY = 'spring-petclinic-api-gateway/src/main/resources/application.yml'
for svc in ('vets-service', 'visits-service', 'customers-service'):
    pc.link('http', 'api-gateway', svc, f'ANY {ROUTE[svc][5:]} (StripPrefix=2)', pc.loc(GWY, f'uri: lb://{svc}'), None,
            'Spring Cloud Gateway route', in_scope=False, reason='declarative gateway prefix route in YAML; no single handler', hop='gateway_route')
pc.write()

# ---------------------------------------------------------------- microSecEnD: apssouza22/java-microservice
ap = Corpus('apssouza22_java-microservice',
    corpus='msecend-apssouza22-java-microservice',
    repo='https://github.com/apssouza22/java-microservice',
    license='MIT',
    languages=['Java (Spring Boot, Spring Cloud Netflix Feign, Spring Cloud Stream Kafka binder)', 'properties config', 'nginx config'],
    sources=[
        MS.format(app='apssouza22_java-microservice') + ': commit 9274eeb from the artifact URLs in the .py model; flows api_gateway -> user_service (feign), oauth -> user_service (feign), reminder -> kafka (Producer Topic todo-mail), kafka -> mailer (Consumer Topic todo-mail), proxy -> api_gateway',
        'manual reading of Feign clients, controllers, Spring Cloud Stream bindings and config-server/src/main/resources/offline-repository/*.properties at the pinned commit',
    ],
    services=[
        {'name': 'api-gateway', 'root': 'api-gateway', 'dfd_name': 'api_gateway'},
        {'name': 'user', 'root': 'user-service', 'dfd_name': 'user_service'},
        {'name': 'reminder', 'root': 'remainder-service', 'dfd_name': 'reminder'},
        {'name': 'mailer', 'root': 'mail-service', 'dfd_name': 'mailer'},
        {'name': 'oauth', 'root': 'oauth-server', 'dfd_name': 'oauth'},
        {'name': 'proxy', 'root': 'proxy', 'dfd_name': 'proxy'},
        {'name': 'todo-infra (shared library, not a service)', 'root': 'todo-infra'},
        {'name': 'config-server', 'root': 'config-server', 'dfd_name': 'config_server'},
        {'name': 'eureka-server', 'root': 'eureka-server', 'dfd_name': 'eureka_server'},
        {'name': 'admin', 'root': 'admin-server', 'dfd_name': 'admin'},
    ],
    notes=[
        'Service names are spring.application.name values (user, reminder, mailer, oauth, api-gateway), which are what Feign/Eureka resolve.',
        'Kafka producer code lives in the shared module todo-infra (EventPublisher.stream -> Source.output channel.send); it is a dependency of remainder-service and mail-service. Only reminder calls stream() (TodoServiceImpl). Destination todo-mail comes from spring.cloud.stream.bindings.output/input.destination in config-server offline-repository properties; mailer consumes with @StreamListener(Sink.INPUT, condition headers type==TodoCreatedEvent).',
        'api-gateway TodoClient is @FeignClient("todo") but no service registers as "todo" (the reminder service is spring.application.name=reminder). Paths match reminder TodoController (/todos, /todos/search). Recorded as out of scope / ambiguous: an extractor that links by service id should not produce it; one that links by path may. Score as dont-care.',
        'api-gateway UserClient.createUser is POST /users on service "user", but user-service has no /users handler (only /accounts...). Listed under unmatched_clients: any link from it is a false positive.',
        'Label disagreements with microSecEnD: (1) reminder -> kafka is traced to TodoSocketController.java:17 @SendTo("/topic/todos"), a STOMP/WebSocket destination, not the Kafka producer; (2) the DFD omits api_gateway -> reminder (the "todo" Feign client) - defensible given the name mismatch, but undocumented; (3) 19 of 77 traceability URLs contain blob/master/master/ and do not resolve. Feign user links and topic todo-mail agree with source.',
    ])
AGC = 'api-gateway/src/main/java/com/apssouza/clients'
UAC = 'user-service/src/main/java/com/apssouza/controllers/AccountController.java'
TC = 'remainder-service/src/main/java/com/apssouza/controllers/TodoController.java'
ap.link('http', 'api-gateway', 'user', 'GET /accounts', ap.loc(f'{AGC}/UserClient.java', 'value = "/accounts", method = RequestMethod.GET'),
        ap.loc(UAC, '    @GetMapping', n=1), '@FeignClient("user"); AccountController @RequestMapping("/accounts") @GetMapping',
        client_invocations=[ap.loc('api-gateway/src/main/java/com/apssouza/services/UserServiceImpl.java', 'userClient.getAll()')])
ap.link('http', 'api-gateway', 'user', 'GET /accounts/search?email=', ap.loc(f'{AGC}/UserClient.java', 'value = "/accounts/search"'),
        ap.loc(UAC, '@GetMapping("search")'), '@FeignClient("user")',
        client_invocations=[ap.loc('api-gateway/src/main/java/com/apssouza/services/UserServiceImpl.java', 'userClient.getUserByEmail(email)')])
ap.link('http', 'oauth', 'user', 'GET /accounts/search?email=', ap.loc('oauth-server/src/main/java/com/apssouza/clients/UserClient.java', 'value = "/accounts/search"'),
        ap.loc(UAC, '@GetMapping("search")'), '@FeignClient("user") in oauth-server',
        client_invocations=[ap.loc('oauth-server/src/main/java/com/apssouza/services/UserServiceImpl.java', 'userClient.getUserByEmail(email)')])
for pat, key_pat, n, contract, inv in [
    ('value = "/todos", method = RequestMethod.GET', '    @GetMapping', 1, 'GET /todos', 'todoClient.getAll()'),
    ('value = "/todos/search"', '@GetMapping("search")', None, 'GET /todos/search?email=', 'todoClient.getTodoByUserEmaill(email)'),
    ('method = RequestMethod.POST', '    @PostMapping', None, 'POST /todos', 'todoClient.createTodo(todo)'),
]:
    ap.link('http', 'api-gateway', 'reminder', contract, ap.loc(f'{AGC}/TodoClient.java', pat), ap.loc(TC, key_pat, n=n),
            '@FeignClient("todo") - path matches reminder TodoController @RequestMapping("/todos")',
            in_scope=False, reason='Feign service id "todo" matches no spring.application.name (reminder registers as "reminder"); target only inferable by path, would fail at runtime',
            label='ambiguous',
            client_invocations=[ap.loc('api-gateway/src/main/java/com/apssouza/services/TodoServiceImpl.java', inv)])
ap.link('messaging', 'reminder', 'mailer', 'kafka-topic:todo-mail',
        ap.loc('todo-infra/src/main/java/com/apssouza/infra/EventPublisher.java', 'channel.send(msg)'),
        ap.loc('mail-service/src/main/java/com/apssouza/mailservice/integration/reminder/EventInput.java', '@StreamListener('),
        'Spring Cloud Stream: Source.output() -> bindings.output.destination=todo-mail (reminder.properties); Sink.INPUT <- bindings.input.destination=todo-mail (mailer.properties); Kafka binder',
        producer_invocation=ap.loc('remainder-service/src/main/java/com/apssouza/services/TodoServiceImpl.java', 'this.publisher.stream(new TodoCreatedEvent(savedTodo))'),
        topic_config=[ap.loc('config-server/src/main/resources/offline-repository/reminder.properties', 'spring.cloud.stream.bindings.output.destination=todo-mail'),
                      ap.loc('config-server/src/main/resources/offline-repository/mailer.properties', 'spring.cloud.stream.bindings.input.destination=todo-mail')])
ap.link('http', 'proxy', 'api-gateway', 'ANY /** (nginx upstream backend)', ap.loc('proxy/default.conf', 'server gateway:8018;'), None,
        'nginx upstream backend { server gateway:8018; } proxy_pass http://backend', in_scope=False,
        reason='declarative reverse-proxy route (config)', hop='gateway_route')
m = ap.meta
m['unmatched_clients'] = [{'kind': 'http', 'from_service': 'api-gateway', 'contract': 'POST /users (service id "user")',
                           'client': ap.loc(f'{AGC}/UserClient.java', 'value = "/users", method = RequestMethod.POST'),
                           'reason': 'no handler for POST /users in user-service; any link produced from this client is a false positive'}]
ap.write()
