"""Stand-ins for Go broker client modules, for tests that run go/types.

Each is the part of a client library's API the tests call, in a module
directory of its own; ``go_mod`` writes a go.mod that requires the modules
and replaces them with these directories, so the download and the
type checker read them without the network. ``needs_go`` skips a test where
the Go toolchain is not installed. The toolchain is required, so the skip is
TEMPORARY: it exists only because the offline acceptance sandbox does not yet
provision Go. Remove it once the acceptance jobs install Go and the sandbox
PATH carries it (#323, #344).
"""

import unittest

from deepthought import go_facts

needs_go = unittest.skipUnless(
    go_facts.toolchain(), "the Go toolchain is not installed"
)

STUBS = {
    "github.com/streadway/amqp": {
        "channel.go": """package amqp

type Table map[string]interface{}
type Publishing struct{ Body []byte }
type Delivery struct{ Body []byte }
type Queue struct {
\tName     string
\tMessages int
}
type Channel struct{}

func (c *Channel) ExchangeDeclare(name, kind string, durable, autoDelete, internal, noWait bool, args Table) error {
\treturn nil
}
func (c *Channel) QueueDeclare(name string, durable, autoDelete, exclusive, noWait bool, args Table) (Queue, error) {
\treturn Queue{}, nil
}
func (c *Channel) QueueBind(name, key, exchange string, noWait bool, args Table) error {
\treturn nil
}
func (c *Channel) Consume(queue, consumer string, autoAck, exclusive, noLocal, noWait bool, args Table) (<-chan Delivery, error) {
\treturn nil, nil
}
func (c *Channel) Publish(exchange, key string, mandatory, immediate bool, msg Publishing) error {
\treturn nil
}
func (c *Channel) PublishWithDeferredConfirmWithContext(ctx interface{}, exchange, key string, mandatory, immediate bool, msg Publishing) (interface{}, error) {
\treturn nil, nil
}
""",
    },
    "github.com/segmentio/kafka-go": {
        "kafka.go": """package kafka

import "context"

type Addr interface{}

func TCP(address ...string) Addr { return nil }

type Message struct {
\tTopic string
\tValue []byte
}
type Writer struct {
\tAddr  Addr
\tTopic string
}

func (w *Writer) WriteMessages(ctx context.Context, msgs ...Message) error { return nil }

type ReaderConfig struct {
\tBrokers     []string
\tTopic       string
\tGroupID     string
\tGroupTopics []string
}
type Reader struct{}

func NewReader(config ReaderConfig) *Reader { return nil }
""",
    },
    "github.com/IBM/sarama": {
        "sarama.go": """package sarama

import "context"

const OffsetNewest int64 = -1

type Encoder interface{}
type ByteEncoder []byte
type StringEncoder string
type ProducerMessage struct {
\tTopic string
\tValue Encoder
}
type SyncProducer interface {
\tSendMessage(msg *ProducerMessage) (int32, int64, error)
}
type PartitionConsumer interface{}
type Consumer interface {
\tConsumePartition(topic string, partition int32, offset int64) (PartitionConsumer, error)
}
type ConsumerGroupHandler interface{}
type ConsumerGroup interface {
\tConsume(ctx context.Context, topics []string, handler ConsumerGroupHandler) error
}
""",
    },
    "github.com/confluentinc/confluent-kafka-go/v2": {
        "kafka/kafka.go": """package kafka

type Event interface{}
type TopicPartition struct{ Topic *string }
type Message struct {
\tTopicPartition TopicPartition
\tValue          []byte
}
type Producer struct{}

func (p *Producer) Produce(msg *Message, deliveryChan chan Event) error { return nil }

type RebalanceCb func(*Consumer, Event) error
type Consumer struct{}

func (c *Consumer) SubscribeTopics(topics []string, cb RebalanceCb) error { return nil }
func (c *Consumer) Subscribe(topic string, cb RebalanceCb) error { return nil }
""",
    },
}
# Router libraries: the route registration API of each.
STUBS["github.com/gin-gonic/gin"] = {
    "gin.go": """package gin

type H map[string]any
type Context struct{}

func (c *Context) ShouldBindJSON(obj any) error { return nil }
func (c *Context) JSON(code int, obj any)        {}

type HandlerFunc func(*Context)

type IRoutes interface {
\tUse(...HandlerFunc) IRoutes
\tHandle(string, string, ...HandlerFunc) IRoutes
\tAny(string, ...HandlerFunc) IRoutes
\tMatch([]string, string, ...HandlerFunc) IRoutes
\tGET(string, ...HandlerFunc) IRoutes
\tPOST(string, ...HandlerFunc) IRoutes
\tPUT(string, ...HandlerFunc) IRoutes
\tPATCH(string, ...HandlerFunc) IRoutes
\tDELETE(string, ...HandlerFunc) IRoutes
\tHEAD(string, ...HandlerFunc) IRoutes
\tOPTIONS(string, ...HandlerFunc) IRoutes
}

type IRouter interface {
\tIRoutes
\tGroup(string, ...HandlerFunc) *RouterGroup
}

type RouterGroup struct{}

func (g *RouterGroup) Group(path string, handlers ...HandlerFunc) *RouterGroup { return g }
func (g *RouterGroup) Use(handlers ...HandlerFunc) IRoutes                    { return g }
func (g *RouterGroup) Handle(method, path string, handlers ...HandlerFunc) IRoutes { return g }
func (g *RouterGroup) Any(path string, handlers ...HandlerFunc) IRoutes       { return g }
func (g *RouterGroup) Match(methods []string, path string, handlers ...HandlerFunc) IRoutes {
\treturn g
}
func (g *RouterGroup) GET(path string, handlers ...HandlerFunc) IRoutes { return g }
func (g *RouterGroup) POST(path string, handlers ...HandlerFunc) IRoutes { return g }
func (g *RouterGroup) PUT(path string, handlers ...HandlerFunc) IRoutes { return g }
func (g *RouterGroup) PATCH(path string, handlers ...HandlerFunc) IRoutes { return g }
func (g *RouterGroup) DELETE(path string, handlers ...HandlerFunc) IRoutes { return g }
func (g *RouterGroup) HEAD(path string, handlers ...HandlerFunc) IRoutes { return g }
func (g *RouterGroup) OPTIONS(path string, handlers ...HandlerFunc) IRoutes { return g }

type Engine struct{ RouterGroup }

func New() *Engine                         { return &Engine{} }
func Default() *Engine                     { return &Engine{} }
func (e *Engine) Use(m ...HandlerFunc) IRoutes { return e }
func (e *Engine) Run(addr ...string) error { return nil }
""",
}
STUBS["github.com/labstack/echo/v4"] = {
    "echo.go": """package echo

type Context interface {
\tBind(i any) error
\tJSON(code int, i any) error
}
type HandlerFunc func(Context) error
type MiddlewareFunc func(HandlerFunc) HandlerFunc
type Route struct{}
type Echo struct{}
type Group struct{}

func New() *Echo { return &Echo{} }
func (e *Echo) Group(prefix string, m ...MiddlewareFunc) *Group { return &Group{} }
func (e *Echo) Any(path string, h HandlerFunc, m ...MiddlewareFunc) []*Route { return nil }
func (e *Echo) Add(method, path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (e *Echo) Match(methods []string, path string, h HandlerFunc, m ...MiddlewareFunc) []*Route {
\treturn nil
}
func (e *Echo) Start(address string) error { return nil }
func (e *Echo) GET(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (e *Echo) POST(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (e *Echo) PUT(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (e *Echo) PATCH(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (e *Echo) DELETE(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (e *Echo) HEAD(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (e *Echo) OPTIONS(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) Group(prefix string, m ...MiddlewareFunc) *Group { return g }
func (g *Group) Any(path string, h HandlerFunc, m ...MiddlewareFunc) []*Route { return nil }
func (g *Group) Add(method, path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) Match(methods []string, path string, h HandlerFunc, m ...MiddlewareFunc) []*Route {
\treturn nil
}
func (g *Group) GET(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) POST(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) PUT(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) PATCH(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) DELETE(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) HEAD(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
func (g *Group) OPTIONS(path string, h HandlerFunc, m ...MiddlewareFunc) *Route { return nil }
""",
}
STUBS["github.com/go-chi/chi/v5"] = {
    "chi.go": """package chi

import "net/http"

type Router interface {
\tUse(middlewares ...func(http.Handler) http.Handler)
\tWith(middlewares ...func(http.Handler) http.Handler) Router
\tGroup(fn func(r Router)) Router
\tRoute(pattern string, fn func(r Router)) Router
\tMount(pattern string, h http.Handler)
\tHandle(pattern string, h http.Handler)
\tHandleFunc(pattern string, h http.HandlerFunc)
\tMethod(method, pattern string, h http.Handler)
\tMethodFunc(method, pattern string, h http.HandlerFunc)
\tGet(pattern string, h http.HandlerFunc)
\tPost(pattern string, h http.HandlerFunc)
\tPut(pattern string, h http.HandlerFunc)
\tPatch(pattern string, h http.HandlerFunc)
\tDelete(pattern string, h http.HandlerFunc)
\tHead(pattern string, h http.HandlerFunc)
\tOptions(pattern string, h http.HandlerFunc)
\tConnect(pattern string, h http.HandlerFunc)
\tTrace(pattern string, h http.HandlerFunc)
}

type Mux struct{}

func NewRouter() *Mux { return &Mux{} }
func NewMux() *Mux    { return &Mux{} }

func (m *Mux) ServeHTTP(w http.ResponseWriter, r *http.Request)            {}
func (m *Mux) Use(middlewares ...func(http.Handler) http.Handler)          {}
func (m *Mux) With(middlewares ...func(http.Handler) http.Handler) Router  { return m }
func (m *Mux) Group(fn func(r Router)) Router                              { return m }
func (m *Mux) Route(pattern string, fn func(r Router)) Router              { return m }
func (m *Mux) Mount(pattern string, h http.Handler)                        {}
func (m *Mux) Handle(pattern string, h http.Handler)                       {}
func (m *Mux) HandleFunc(pattern string, h http.HandlerFunc)               {}
func (m *Mux) Method(method, pattern string, h http.Handler)               {}
func (m *Mux) MethodFunc(method, pattern string, h http.HandlerFunc)       {}
func (m *Mux) Get(pattern string, h http.HandlerFunc) {}
func (m *Mux) Post(pattern string, h http.HandlerFunc) {}
func (m *Mux) Put(pattern string, h http.HandlerFunc) {}
func (m *Mux) Patch(pattern string, h http.HandlerFunc) {}
func (m *Mux) Delete(pattern string, h http.HandlerFunc) {}
func (m *Mux) Head(pattern string, h http.HandlerFunc) {}
func (m *Mux) Options(pattern string, h http.HandlerFunc) {}
func (m *Mux) Connect(pattern string, h http.HandlerFunc) {}
func (m *Mux) Trace(pattern string, h http.HandlerFunc) {}
""",
}
STUBS["github.com/gofiber/fiber/v2"] = {
    "app.go": """package fiber

type Ctx struct{}

func (c *Ctx) BodyParser(out any) error { return nil }
func (c *Ctx) JSON(data any) error      { return nil }
func (c *Ctx) Next() error              { return nil }

type Handler = func(*Ctx) error

type Router interface {
\tUse(args ...any) Router
\tAdd(method, path string, handlers ...Handler) Router
\tAll(path string, handlers ...Handler) Router
\tGroup(prefix string, handlers ...Handler) Router
\tRoute(prefix string, fn func(router Router), name ...string) Router
\tGet(path string, handlers ...Handler) Router
\tHead(path string, handlers ...Handler) Router
\tPost(path string, handlers ...Handler) Router
\tPut(path string, handlers ...Handler) Router
\tDelete(path string, handlers ...Handler) Router
\tConnect(path string, handlers ...Handler) Router
\tOptions(path string, handlers ...Handler) Router
\tTrace(path string, handlers ...Handler) Router
\tPatch(path string, handlers ...Handler) Router
}

type Config struct{}
type App struct{}
type Group struct{}

func New(config ...Config) *App { return &App{} }
func (app *App) Listen(addr string) error { return nil }
func (app *App) Use(args ...any) Router { return app }
func (app *App) Add(method, path string, handlers ...Handler) Router { return app }
func (app *App) All(path string, handlers ...Handler) Router { return app }
func (app *App) Group(prefix string, handlers ...Handler) Router { return &Group{} }
func (app *App) Route(prefix string, fn func(router Router), name ...string) Router { return app }
func (app *App) Get(path string, handlers ...Handler) Router { return app }
func (app *App) Head(path string, handlers ...Handler) Router { return app }
func (app *App) Post(path string, handlers ...Handler) Router { return app }
func (app *App) Put(path string, handlers ...Handler) Router { return app }
func (app *App) Delete(path string, handlers ...Handler) Router { return app }
func (app *App) Connect(path string, handlers ...Handler) Router { return app }
func (app *App) Options(path string, handlers ...Handler) Router { return app }
func (app *App) Trace(path string, handlers ...Handler) Router { return app }
func (app *App) Patch(path string, handlers ...Handler) Router { return app }
func (g *Group) Use(args ...any) Router { return g }
func (g *Group) Add(method, path string, handlers ...Handler) Router { return g }
func (g *Group) All(path string, handlers ...Handler) Router { return g }
func (g *Group) Group(prefix string, handlers ...Handler) Router { return g }
func (g *Group) Route(prefix string, fn func(router Router), name ...string) Router { return g }
func (g *Group) Get(path string, handlers ...Handler) Router { return g }
func (g *Group) Head(path string, handlers ...Handler) Router { return g }
func (g *Group) Post(path string, handlers ...Handler) Router { return g }
func (g *Group) Put(path string, handlers ...Handler) Router { return g }
func (g *Group) Delete(path string, handlers ...Handler) Router { return g }
func (g *Group) Connect(path string, handlers ...Handler) Router { return g }
func (g *Group) Options(path string, handlers ...Handler) Router { return g }
func (g *Group) Trace(path string, handlers ...Handler) Router { return g }
func (g *Group) Patch(path string, handlers ...Handler) Router { return g }
""",
}
STUBS["github.com/gofiber/fiber/v3"] = {
    "app.go": """package fiber

type Bind struct{}

func (b *Bind) Body(out any) error { return nil }

type Ctx interface {
\tBind() *Bind
\tJSON(data any, ctype ...string) error
\tNext() error
}

type Handler = func(Ctx) error

type Router interface {
\tUse(args ...any) Router
\tAdd(methods []string, path string, handler Handler, middleware ...Handler) Router
\tAll(path string, handler Handler, middleware ...Handler) Router
\tGroup(prefix string, handlers ...Handler) Router
\tRoute(path string) Register
\tGet(path string, handler Handler, middleware ...Handler) Router
\tHead(path string, handler Handler, middleware ...Handler) Router
\tPost(path string, handler Handler, middleware ...Handler) Router
\tPut(path string, handler Handler, middleware ...Handler) Router
\tDelete(path string, handler Handler, middleware ...Handler) Router
\tConnect(path string, handler Handler, middleware ...Handler) Router
\tOptions(path string, handler Handler, middleware ...Handler) Router
\tTrace(path string, handler Handler, middleware ...Handler) Router
\tPatch(path string, handler Handler, middleware ...Handler) Router
}

type Register interface {
\tAll(handler Handler, middleware ...Handler) Register
\tAdd(methods []string, handler Handler, middleware ...Handler) Register
\tRoute(path string) Register
\tGet(handler Handler, middleware ...Handler) Register
\tHead(handler Handler, middleware ...Handler) Register
\tPost(handler Handler, middleware ...Handler) Register
\tPut(handler Handler, middleware ...Handler) Register
\tDelete(handler Handler, middleware ...Handler) Register
\tConnect(handler Handler, middleware ...Handler) Register
\tOptions(handler Handler, middleware ...Handler) Register
\tTrace(handler Handler, middleware ...Handler) Register
\tPatch(handler Handler, middleware ...Handler) Register
}

type Config struct{}
type App struct{}
type Group struct{}

func New(config ...Config) *App { return &App{} }
func (app *App) Listen(addr string) error { return nil }
func (app *App) Use(args ...any) Router { return app }
func (app *App) Add(methods []string, path string, handler Handler, middleware ...Handler) Router {
\treturn app
}
func (app *App) All(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Group(prefix string, handlers ...Handler) Router { return &Group{} }
func (app *App) Route(path string) Register { return nil }
func (app *App) Get(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Head(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Post(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Put(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Delete(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Connect(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Options(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Trace(path string, handler Handler, middleware ...Handler) Router { return app }
func (app *App) Patch(path string, handler Handler, middleware ...Handler) Router { return app }
func (g *Group) Use(args ...any) Router { return g }
func (g *Group) Add(methods []string, path string, handler Handler, middleware ...Handler) Router {
\treturn g
}
func (g *Group) All(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Group(prefix string, handlers ...Handler) Router { return g }
func (g *Group) Route(path string) Register { return nil }
func (g *Group) Get(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Head(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Post(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Put(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Delete(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Connect(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Options(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Trace(path string, handler Handler, middleware ...Handler) Router { return g }
func (g *Group) Patch(path string, handler Handler, middleware ...Handler) Router { return g }
""",
}
STUBS["github.com/rabbitmq/amqp091-go"] = {
    "channel.go": STUBS["github.com/streadway/amqp"]["channel.go"].replace(
        "package amqp\n", "package amqp091\n", 1
    )
}


STUBS["github.com/go-chi/render"] = {
    "render.go": """package render

import (
\t"io"
\t"net/http"
)

type Renderer interface {
\tRender(w http.ResponseWriter, r *http.Request) error
}
type Binder interface {
\tBind(r *http.Request) error
}

func JSON(w http.ResponseWriter, r *http.Request, v interface{})          {}
func Respond(w http.ResponseWriter, r *http.Request, v interface{})       {}
func Render(w http.ResponseWriter, r *http.Request, v Renderer) error     { return nil }
func Status(r *http.Request, status int)                                  {}
func Bind(r *http.Request, v Binder) error                                { return nil }
func Decode(r *http.Request, v interface{}) error                         { return nil }
func DecodeJSON(r io.Reader, v interface{}) error                         { return nil }
""",
}


def stub_directory(path):
    return "gostubs/" + path.replace("/", "_")


def go_stub_files():
    """{relative path: text} of every stub module, under gostubs/."""
    files = {}
    for path, sources in STUBS.items():
        directory = stub_directory(path)
        files[f"{directory}/go.mod"] = f"module {path}\n\ngo 1.22\n"
        for name, text in sources.items():
            files[f"{directory}/{name}"] = text
    return files


def _version(path):
    """v0.0.0, or vN.0.0 for a module path ending in a major version /vN."""
    last = path.rpartition("/")[2]
    major = last[1:] if last[:1] == "v" and last[1:].isdigit() else "0"
    return f"v{major}.0.0"


def go_mod(module, *requires, depth=1):
    """A go.mod for ``module``, ``depth`` directories below the root,
    requiring the stub modules ``requires``."""
    up = "../" * depth
    lines = [f"module {module}", "", "go 1.22", ""]
    lines += [f"require {path} {_version(path)}" for path in requires]
    lines += [f"replace {path} => {up}{stub_directory(path)}" for path in requires]
    return "\n".join(lines) + "\n"
