"""ServiceContractTool: HTTP routes servers declare and the requests clients make.

Routes and requests are read from Java/Spring, Python, JavaScript/TypeScript
and Go sources (see ``deepthought.service_contracts``). Each route is an
HttpRoute node linked from its handler by HANDLES (from its file when the
handler is not a declaration); each request that one rule ties to exactly one
route links its calling declaration to it by REQUESTS. The handlers and
callers are the tree-sitter declarations of the same files, with the same
identities, so a revision that also contains tree-sitter output joins them.
"""

from hashlib import sha256
from pathlib import PurePosixPath
import functools
import json
import os
import posixpath
import importlib.util
import itertools
import re
import time

from .._version import __version__
from ..data_contracts import (
    read_tsconfig_paths,
    resolve_script_alias,
    resolve_script_module,
)
from ..engine import analyze_repository
from ..service_contracts import grpc as grpc_contracts
from ..service_contracts import resolve as contract_rules
from ..service_contracts.cloud_stream import MavenModules, extract_stream
from ..service_contracts.deployed import (
    Deployments,
    command_line,
    deployed_units,
    locust_host,
)
from ..service_contracts.gateways import (
    nginx_routes,
    ocelot_routes,
    spring_gateway_routes,
    yarp_routes,
)
from ..service_contracts.graphql import (
    CLIENTS as GRAPHQL_CLIENTS,
    SERVERS as GRAPHQL_SERVERS,
    GraphqlDeclaration,
    RESOLVER_MAP_SERVERS,
    client_operations,
    extract_nest_resolvers,
    extract_resolver_maps,
    follow_reference,
    operation_fields,
    schema_fields,
    template_schemas,
)
from ..service_contracts.http import PLACEHOLDER
from ..service_contracts.model import Unread
from ..service_contracts.messaging import (
    MessageEndpoint,
    MessageUnread,
    extract_messaging,
    stomp_prefixes,
)
from ..service_contracts.openapi import generated_apis
from ..service_contracts.aspnet import extract_aspnet
from ..service_contracts.csharp_http import (
    extract_csharp_requests,
    extract_refit_requests,
    global_usings,
    typed_client_bases,
    typed_client_registrations,
)
from ..service_contracts.php import extract_php
from ..service_contracts.spring import (
    SpringConfig,
    _module_root,
    extract_spring,
    java_constants,
    java_constructions,
)
from ..service_contracts.script_constants import (
    ScriptCalls,
    ScriptConstants,
    angular_defines,
)
from ..service_contracts.web import (
    compose_express_prefixes,
    compose_python_prefixes,
    express_routers,
    extract_web,
    nest_settings,
    python_routers,
)
from ..syntax import identifier, make_parser
from .base import AnalysisTool, ToolCapability, ToolMetadata
from .contracts import (
    NodeData,
    RelationshipData,
    StandardizedOutput,
    ToolExecutionMetadata,
)
from .evidence import EvidenceRule, register_rule
from .registry import register_tool

SUPPORTED_LANGUAGES = (
    "csharp",
    "go",
    "java",
    "javascript",
    "php",
    "python",
    "typescript",
)

ROUTE_RULES = {
    "aspnet-controller": register_rule(
        EvidenceRule(
            "service-contracts.aspnet-controller-route",
            "pattern_matched",
            "A method of an ASP.NET Core controller (a class with "
            "[ApiController], deriving from Controller or ControllerBase, or "
            "named ...Controller) carries [HttpGet], [HttpPost], ... or "
            "[Route] with a literal template, joined with the class's [Route] "
            "templates; [controller], [action] and [area] are replaced. "
            "Conventional routes (MapControllerRoute) are not read, and paths "
            "are matched with the case they are written in.",
        )
    ),
    "aspnet-minimal-api": register_rule(
        EvidenceRule(
            "service-contracts.aspnet-minimal-api-route",
            "pattern_matched",
            "A MapGet, MapPost, MapPut, MapDelete, MapPatch or MapMethods call "
            "with a literal template gives this route, under the literal "
            "prefixes of the MapGroup calls its receiver is built from; a "
            "RouteGroupBuilder or IEndpointRouteBuilder parameter has an "
            "unknown prefix.",
        )
    ),
    "symfony": register_rule(
        EvidenceRule(
            "service-contracts.symfony-route",
            "pattern_matched",
            "A method of a PHP class carries a Route annotation (in its "
            "docblock) or attribute, imported from Symfony's Routing "
            "Annotation or Attribute namespace, whose literal path, joined "
            "with the class's own Route path, and methods give this route; "
            "routes in YAML, XML or PHP config files and the kernel's prefix "
            "are not read.",
        )
    ),
    "spring-mvc": register_rule(
        EvidenceRule(
            "service-contracts.spring-route-mapping",
            "pattern_matched",
            "A method of a Spring controller (or an interface) carries a mapping "
            "annotation imported from org.springframework.web.bind.annotation whose "
            "method and literal path, joined with the type's @RequestMapping path, "
            "give this route. The servlet context path, gateways and routes "
            "registered in code are not read.",
        )
    ),
    "flask": register_rule(
        EvidenceRule(
            "service-contracts.flask-route",
            "pattern_matched",
            "A function carries a route decorator of a Flask app or Blueprint "
            "bound once in the file to flask.Flask or flask.Blueprint; the "
            "Blueprint's url_prefix is joined, but not the prefix given when it "
            "is registered.",
        )
    ),
    "fastapi": register_rule(
        EvidenceRule(
            "service-contracts.fastapi-route",
            "pattern_matched",
            "A function carries a route decorator of a FastAPI app or APIRouter "
            "bound once in the file; the APIRouter's own prefix is joined, but not "
            "the prefix given to include_router.",
        )
    ),
    "express": register_rule(
        EvidenceRule(
            "service-contracts.express-route",
            "pattern_matched",
            "A method call (get, post, ..., all) on an Express app or Router bound "
            "once in the file from the express module declares this path; a "
            "prefix given to app.use is not read.",
        )
    ),
    "nestjs": register_rule(
        EvidenceRule(
            "service-contracts.nestjs-route",
            "pattern_matched",
            "A method of a class decorated @Controller(path) carries a route "
            "decorator (@Get, @Post, ..., @All) imported from @nestjs/common; "
            "the literal controller and method paths are joined under the "
            "package's single literal setGlobalPrefix, if any. Versioned "
            "routes, RouterModule paths and prefixes with exclusions are not "
            "read.",
        )
    ),
    "go-net-http": register_rule(
        EvidenceRule(
            "service-contracts.go-handle-func",
            "pattern_matched",
            "A HandleFunc or Handle call in a file importing net/http or "
            "gorilla/mux declares this path, with methods from a chained "
            ".Methods(...) or a Go 1.22 'METHOD /path' pattern; a base path "
            "variable before the path is assumed to add no segments.",
        )
    ),
    "spring-websocket": register_rule(
        EvidenceRule(
            "service-contracts.spring-websocket-endpoint",
            "pattern_matched",
            "A method with a StompEndpointRegistry or WebSocketHandlerRegistry "
            "parameter (from org.springframework.web.socket.config.annotation) "
            "calls addEndpoint or addHandler on it with this literal path; the "
            "route is the WebSocket handshake (GET), under the servlet context "
            "path, handled by that method. SockJS fallback paths below it are "
            "not listed.",
        )
    ),
    "jakarta-websocket": register_rule(
        EvidenceRule(
            "service-contracts.jakarta-websocket-endpoint",
            "pattern_matched",
            "A class carries @ServerEndpoint (javax or jakarta "
            ".websocket.server) with this literal path; the route is the "
            "WebSocket handshake (GET), under the servlet context path, "
            "handled by the class.",
        )
    ),
    "openapi-spring": register_rule(
        EvidenceRule(
            "service-contracts.openapi-spring-route",
            "pattern_matched",
            "An OpenAPI operation is generated into a Spring interface by an "
            "openapi-generator execution (generator spring, version 7 or later, "
            "interfaceOnly, no base path on the interface) of the Maven plugin "
            "in a pom.xml or the Gradle plugin in a build.gradle(.kts) of the "
            "repository, and a controller implementing that "
            "interface overrides the operation's method; the path is the "
            "operation's, joined with the controller's @RequestMapping path and "
            "the servlet context path.",
        )
    ),
}


def _message_rule(rule_id, description):
    return register_rule(EvidenceRule(rule_id, "pattern_matched", description))


_SAME_NAME = (
    " Publishers and consumers meet at the destination node with the same "
    "broker and name; consumer groups, brokers and clusters are not checked."
)
MESSAGE_RULES = {
    ("spring-kafka", "publish"): _message_rule(
        "service-contracts.spring-kafka-send",
        "KafkaTemplate.send names this topic as a literal or folded value, or "
        "as the first argument of a ProducerRecord, on a name declared only "
        "with the Spring KafkaTemplate type." + _SAME_NAME,
    ),
    ("spring-kafka", "consume"): _message_rule(
        "service-contracts.spring-kafka-listener",
        "A method carries @KafkaListener from org.springframework.kafka whose "
        "topics name this topic." + _SAME_NAME,
    ),
    ("spring-amqp", "publish"): _message_rule(
        "service-contracts.spring-amqp-send",
        "RabbitTemplate.convertAndSend or send with an exchange and routing key "
        "names this exchange/key destination." + _SAME_NAME,
    ),
    ("spring-amqp", "consume"): _message_rule(
        "service-contracts.spring-rabbit-listener",
        "A method carries @RabbitListener whose queues include this queue; it "
        "receives the default exchange's messages keyed with the queue name."
        + _SAME_NAME,
    ),
    ("kafka-python", "publish"): _message_rule(
        "service-contracts.kafka-python-send",
        "send on a name bound once to kafka.KafkaProducer names this topic."
        + _SAME_NAME,
    ),
    ("kafka-python", "consume"): _message_rule(
        "service-contracts.kafka-python-consumer",
        "kafka.KafkaConsumer is constructed with this topic." + _SAME_NAME,
    ),
    ("pika", "publish"): _message_rule(
        "service-contracts.pika-publish",
        "basic_publish in a file importing pika names this exchange and routing "
        "key." + _SAME_NAME,
    ),
    ("pika", "consume"): _message_rule(
        "service-contracts.pika-consume",
        "basic_consume in a file importing pika consumes a queue that receives "
        "this destination: the default exchange keyed with the queue name, or "
        "a queue_bind of that queue in the same file." + _SAME_NAME,
    ),
    ("kafka-go", "publish"): _message_rule(
        "service-contracts.kafka-go-writer",
        "A segmentio/kafka-go Writer, WriterConfig or Message literal written "
        "with Topic names this topic; the producer is where the literal is "
        "written." + _SAME_NAME,
    ),
    ("kafka-go", "consume"): _message_rule(
        "service-contracts.kafka-go-reader",
        "A segmentio/kafka-go ReaderConfig literal written with Topic or "
        "GroupTopics names this topic; the consumer is where the literal is "
        "written." + _SAME_NAME,
    ),
    ("sarama", "publish"): _message_rule(
        "service-contracts.sarama-producer-message",
        "A sarama ProducerMessage literal written with Topic names this "
        "topic." + _SAME_NAME,
    ),
    ("sarama", "consume"): _message_rule(
        "service-contracts.sarama-consume",
        "ConsumePartition(topic, ...) or a consumer group's Consume(ctx, "
        "[]string{...}, handler), in a file importing sarama, names this "
        "topic." + _SAME_NAME,
    ),
    ("confluent-kafka-go", "publish"): _message_rule(
        "service-contracts.confluent-kafka-go-produce",
        "A confluent-kafka-go TopicPartition literal written with Topic names "
        "this topic." + _SAME_NAME,
    ),
    ("confluent-kafka-go", "consume"): _message_rule(
        "service-contracts.confluent-kafka-go-subscribe",
        "Subscribe(topic, ...) or SubscribeTopics([]string{...}, ...), in a "
        "file importing confluent-kafka-go, names this topic; a topic "
        "starting with ^ is a pattern and is not read." + _SAME_NAME,
    ),
    ("amqplib", "publish"): _message_rule(
        "service-contracts.amqplib-publish",
        "publish(exchange, routingKey, ...) or sendToQueue(queue, ...) (the "
        "default exchange keyed with the queue name), in a file importing "
        "amqplib, on a name bound to an amqplib channel: assigned "
        "createChannel() or createConfirmChannel() everywhere in the file, or "
        "the channel parameter of its callback or .then, names this exchange "
        "and routing key." + _SAME_NAME,
    ),
    ("amqplib", "consume"): _message_rule(
        "service-contracts.amqplib-consume",
        "consume(queue, ...) on an amqplib channel, in a file importing "
        "amqplib, consumes a queue that receives this destination: the default "
        "exchange keyed with the queue name, or a bindQueue of that queue in "
        "the same file." + _SAME_NAME,
    ),
    ("nestjs-microservices", "consume"): _message_rule(
        "service-contracts.nestjs-message-pattern",
        "A method of a class decorated @Controller (@nestjs/common) carries "
        "@MessagePattern(pattern) or @EventPattern(pattern) from "
        "@nestjs/microservices; the destination is message: or event: and "
        "the pattern as Nest's transformPatternToRoute writes it (object keys "
        "sorted)." + _SAME_NAME,
    ),
    ("nestjs-microservices", "publish"): _message_rule(
        "service-contracts.nestjs-client-send",
        "send(pattern, ...) (a message) or emit(pattern, ...) (an event) on a "
        "name declared in the file only with a ClientProxy type from "
        "@nestjs/microservices (ClientProxy, ClientKafka, ClientRMQ and the "
        "like) names this pattern." + _SAME_NAME,
    ),
    ("kafkajs", "publish"): _message_rule(
        "service-contracts.kafkajs-send",
        "send({topic}) or sendBatch({topicMessages: [{topic}]}) on a name that "
        "every assignment in the file binds to producer() of a kafkajs Kafka "
        "names this topic." + _SAME_NAME,
    ),
    ("kafkajs", "consume"): _message_rule(
        "service-contracts.kafkajs-subscribe",
        "subscribe({topic}) or subscribe({topics: [...]}) on a name that every "
        "assignment in the file binds to consumer() of a kafkajs Kafka names "
        "this topic." + _SAME_NAME,
    ),
    ("go-amqp", "publish"): _message_rule(
        "service-contracts.go-amqp-publish",
        "Publish in a file importing an amqp client package names this exchange "
        "and routing key." + _SAME_NAME,
    ),
    ("go-amqp", "consume"): _message_rule(
        "service-contracts.go-amqp-consume",
        "Consume in a file importing an amqp client package consumes a queue "
        "that receives this destination: the default exchange keyed with the "
        "queue name, or a QueueBind of that queue in the same file." + _SAME_NAME,
    ),
    ("spring-stomp", "consume"): _message_rule(
        "service-contracts.spring-stomp-message-mapping",
        "A method carries @MessageMapping (org.springframework.messaging."
        "handler.annotation) with this literal path, joined with its class's "
        "@MessageMapping path, under each application destination prefix that "
        "its module's MessageBrokerRegistry sets (none when it sets none)."
        + _SAME_NAME,
    ),
    ("spring-stomp", "publish"): _message_rule(
        "service-contracts.spring-stomp-send",
        "A @MessageMapping method's @SendTo names this destination, a "
        "@SubscribeMapping method answers a subscription to it (under the "
        "application prefix), or convertAndSend on a name declared only as a "
        "Spring SimpMessagingTemplate or SimpMessageSendingOperations names it."
        + _SAME_NAME,
    ),
    ("stompjs", "publish"): _message_rule(
        "service-contracts.stompjs-send",
        "send(destination, ...) or publish({destination}) on a name that every "
        "declaration and assignment in the file binds to Stomp.over, "
        "Stomp.client, webstomp or a @stomp/stompjs Client (or the StompJs.Client "
        "browser global), or types as that Client, or on AngularJS ngStomp's "
        "$stomp, names this destination." + _SAME_NAME,
    ),
    ("stompjs", "consume"): _message_rule(
        "service-contracts.stompjs-subscribe",
        "subscribe(destination, ...) on a name that every declaration and "
        "assignment in the file binds to Stomp.over, Stomp.client, webstomp or "
        "a @stomp/stompjs Client (or the StompJs.Client browser global), or "
        "types as that Client, or on AngularJS ngStomp's $stomp, names this "
        "destination." + _SAME_NAME,
    ),
    ("rx-stomp", "publish"): _message_rule(
        "service-contracts.rx-stomp-publish",
        "publish({destination}) on a name that every declaration and "
        "assignment in the file binds to, or types as, a @stomp/rx-stomp "
        "RxStomp names this destination." + _SAME_NAME,
    ),
    ("rx-stomp", "consume"): _message_rule(
        "service-contracts.rx-stomp-watch",
        "watch(destination) or watch({destination}) on a name that every "
        "declaration and assignment in the file binds to, or types as, a "
        "@stomp/rx-stomp RxStomp names this destination." + _SAME_NAME,
    ),
    ("ng2-stomp-service", "publish"): _message_rule(
        "service-contracts.ng2-stomp-service-send",
        "send(destination, body) on a name that every declaration and "
        "assignment in the file binds to, or types as, an ng2-stomp-service "
        "StompService (an injected constructor parameter) names this "
        "destination." + _SAME_NAME,
    ),
    ("ng2-stomp-service", "consume"): _message_rule(
        "service-contracts.ng2-stomp-service-subscribe",
        "subscribe(destination, callback) on a name that every declaration and "
        "assignment in the file binds to, or types as, an ng2-stomp-service "
        "StompService names this destination." + _SAME_NAME,
    ),
    ("graphql-subscriptions", "publish"): _message_rule(
        "service-contracts.graphql-pubsub-publish",
        "publish(topic, ...) on a name that every assignment in the file binds "
        "to a graphql-subscriptions PubSub (or the PubSub re-exported by "
        "apollo-server, graphql-yoga or @nestjs/graphql) names this topic. An "
        "in-memory PubSub delivers only within its own instance, so the "
        "destination is the file, the name and the topic." + _SAME_NAME,
    ),
    ("graphql-subscriptions", "consume"): _message_rule(
        "service-contracts.graphql-pubsub-subscribe",
        "asyncIterator(topic), asyncIterableIterator(topic) or subscribe(topic, "
        "...) on a name that every assignment in the file binds to a "
        "graphql-subscriptions PubSub names this topic; an array names each "
        "topic. The destination is the file, the name and the topic." + _SAME_NAME,
    ),
    ("graphql-redis-subscriptions", "publish"): _message_rule(
        "service-contracts.graphql-redis-pubsub-publish",
        "publish(channel, ...) on a name that every assignment in the file "
        "binds to a graphql-redis-subscriptions RedisPubSub names this Redis "
        "channel." + _SAME_NAME,
    ),
    ("graphql-redis-subscriptions", "consume"): _message_rule(
        "service-contracts.graphql-redis-pubsub-subscribe",
        "asyncIterator(channel), asyncIterableIterator(channel) or "
        "subscribe(channel, ...) on a name that every assignment in the file "
        "binds to a graphql-redis-subscriptions RedisPubSub names this Redis "
        "channel; an array names each channel." + _SAME_NAME,
    ),
    ("spring-cloud-stream", "publish"): _message_rule(
        "service-contracts.spring-cloud-stream-output",
        "Code sends to a Spring Cloud Stream binding (Source.output(), @SendTo "
        "or StreamBridge.send) in a service that runs it (its own module, or a "
        "module whose pom.xml depends on it), and that service binds the "
        "binding to this destination by spring.cloud.stream.bindings.<name>."
        "destination, or by the binding's name, through its Kafka or RabbitMQ "
        "binder." + _SAME_NAME,
    ),
    ("spring-cloud-stream", "consume"): _message_rule(
        "service-contracts.spring-cloud-stream-listener",
        "A @StreamListener method listens on a Spring Cloud Stream binding in a "
        "service that runs it, and that service binds the binding to this "
        "destination by spring.cloud.stream.bindings.<name>.destination, or by "
        "the binding's name, through its Kafka or RabbitMQ binder." + _SAME_NAME,
    ),
    ("spring-cloud-function", "publish"): _message_rule(
        "service-contracts.spring-cloud-function-output",
        "A Supplier or Function @Bean (java.util.function) in a service that runs "
        "it is bound by Spring Cloud Stream: the service's "
        "spring.cloud.function.definition lists it, or it is the service's only "
        "functional bean. Its <bean>-out-0 binding (or the name "
        "spring.cloud.stream.function.bindings gives it) is bound to this "
        "destination by spring.cloud.stream.bindings.<name>.destination, or by "
        "the binding's name, through its Kafka or RabbitMQ binder." + _SAME_NAME,
    ),
    ("spring-cloud-function", "consume"): _message_rule(
        "service-contracts.spring-cloud-function-input",
        "A Consumer or Function @Bean (java.util.function) in a service that runs "
        "it is bound by Spring Cloud Stream: the service's "
        "spring.cloud.function.definition lists it, or it is the service's only "
        "functional bean. Its <bean>-in-0 binding (or the name "
        "spring.cloud.stream.function.bindings gives it) is bound to this "
        "destination by spring.cloud.stream.bindings.<name>.destination, or by "
        "the binding's name, through its Kafka or RabbitMQ binder." + _SAME_NAME,
    ),
}

REQUEST_RULES = {
    contract_rules.HTTP_SERVICE_PATH: register_rule(
        EvidenceRule(
            contract_rules.HTTP_SERVICE_PATH,
            "pattern_matched",
            "The request names a service (a Feign client's name, or the URL's "
            "literal host) equal to exactly one service's spring.application.name "
            "or deployment name, or to a host name that only that service's "
            "deployments answer to (a Compose service name, or a Kubernetes "
            "Service selecting its workload), and its method and path fit this "
            "route of that service more "
            "specifically than any other; the value of unknown URL parts is not "
            "traced.",
        )
    ),
    contract_rules.HTTP_PATH: register_rule(
        EvidenceRule(
            contract_rules.HTTP_PATH,
            "pattern_matched",
            "The request names no known service, and its method and path fit "
            "this route more specifically than any other route in the "
            "repository; the host, base URL and gateway are not checked.",
        )
    ),
    contract_rules.HTTP_GATEWAY: register_rule(
        EvidenceRule(
            contract_rules.HTTP_GATEWAY,
            "pattern_matched",
            "The request has no literal host and names no known service, its "
            "path starts with the literal prefix of a gateway route (a Spring "
            "Cloud Gateway Path predicate or an nginx location with "
            "proxy_pass), and, with that prefix replaced as the route "
            "declares, its method and path fit this route of the route's "
            "target service (or of any service, when the target is not a "
            "known service name) more specifically than any other.",
        )
    ),
}
_LOCUST_HOST = (
    "A Locust request written without a host takes the --host (or -H) of "
    "the locust command line that every deployment running its file runs "
    "(a Kubernetes container's command and args, else the ENTRYPOINT and "
    "CMD of the Dockerfile in its build context), with ${VAR} replaced by "
    "the literal value that deployment sets; all of them give one host. "
)
LOCUST_HOST_RULES = {
    rule: register_rule(
        EvidenceRule(
            f"service-contracts.locust-host-{rule.rpartition('.http-')[2]}",
            "pattern_matched",
            _LOCUST_HOST + REQUEST_RULES[rule].description,
        )
    )
    for rule in REQUEST_RULES
}
# A destination passed to the function around the site by its callers.
ARGUMENT_RULE = _message_rule(
    "service-contracts.destination-argument",
    "The site's destination is built from parameters of the module-level "
    "function around it, which nothing in the function assigns again, and "
    "a call of that function (in its own module, or in a module importing it "
    "by name or as a namespace through a relative path or tsconfig paths "
    "alias) gives this value with its arguments, folded in the calling "
    "module, in place of the parameters. The site itself is read by the rule "
    "in site_rule; argument_from lists the calls." + _SAME_NAME,
)
# How the service that runs a module is named (SpringConfig.identity).
IDENTITY_RULES = {
    "spring-application-name": register_rule(
        EvidenceRule(
            "service-contracts.service-spring-application-name",
            "pattern_matched",
            "The module's spring.application.name names the service; without "
            "one, the SPRING_APPLICATION_NAME, or the spring.application.name "
            "under the active profiles in the SPRING_CONFIG_LOCATION files, "
            "that every Compose service building the module gives it.",
        )
    ),
    "compose-service": register_rule(
        EvidenceRule(
            "service-contracts.service-compose-name",
            "pattern_matched",
            "The module sets no spring.application.name, and every Compose "
            "service whose build context is the module's directory has this "
            "name.",
        )
    ),
    "module-name": register_rule(
        EvidenceRule(
            "service-contracts.service-module-name",
            "pattern_matched",
            "Compose services with different names build the module and none "
            "names it: the service is named after the module's directory.",
        )
    ),
}
_OPENAPI_CLIENT = (
    "A call of a method of a Java API class that an "
    "openapi-generator execution (generator java, version 4 or later) of the "
    "Maven plugin in a pom.xml or the Gradle plugin in a build.gradle(.kts) "
    "of the repository makes from a spec sends that "
    "operation: on a name declared once in the file with that class as its "
    "type, or on a new instance. The method and path are the operation's, "
    "under the spec's first server URL, or under an unknown base when the "
    "module calls setBasePath, setServers or setServerIndex. "
)
OPENAPI_REQUEST_RULES = {
    contract_rules.HTTP_SERVICE_PATH: register_rule(
        EvidenceRule(
            "service-contracts.openapi-client-service-path",
            "pattern_matched",
            _OPENAPI_CLIENT
            + "The server URL's literal host equals exactly one service's "
            "spring.application.name, and the request fits this route of that "
            "service more specifically than any other.",
        )
    ),
    contract_rules.HTTP_PATH: register_rule(
        EvidenceRule(
            "service-contracts.openapi-client-path",
            "pattern_matched",
            _OPENAPI_CLIENT
            + "The host names no known service, and the request fits this "
            "route more specifically than any other route in the repository; "
            "the host and port are not checked.",
        )
    ),
    contract_rules.HTTP_GATEWAY: register_rule(
        EvidenceRule(
            "service-contracts.openapi-client-gateway",
            "pattern_matched",
            _OPENAPI_CLIENT
            + "With an unknown base, the path starts with the literal prefix "
            "of a gateway route and, with that prefix replaced, fits this "
            "route of the route's target service more specifically than any "
            "other.",
        )
    ),
}
_OPENAPI_FEIGN = (
    "A call of a method of a Feign client <Api>Client that an "
    "openapi-generator execution (generator spring, library spring-cloud, "
    "version 7 or later, interfaceOnly false) in the repository makes from a "
    "spec sends that operation: on a name declared once in the file with that "
    "class as its type. The method and path are the operation's, under the "
    "client's base, which is a property or service discovery and so is not "
    "fixed. "
)
OPENAPI_FEIGN_REQUEST_RULES = {
    contract_rules.HTTP_PATH: register_rule(
        EvidenceRule(
            "service-contracts.openapi-feign-client-path",
            "pattern_matched",
            _OPENAPI_FEIGN
            + "The request fits this route more specifically than any other "
            "route in the repository.",
        )
    ),
    contract_rules.HTTP_GATEWAY: register_rule(
        EvidenceRule(
            "service-contracts.openapi-feign-client-gateway",
            "pattern_matched",
            _OPENAPI_FEIGN
            + "The path starts with the literal prefix of a gateway route and, "
            "with that prefix replaced, fits this route of the route's target "
            "service more specifically than any other.",
        )
    ),
}
GRPC_SERVER_RULES = {
    rule_id: register_rule(EvidenceRule(rule_id, "pattern_matched", description))
    for rule_id, description in (
        (
            grpc_contracts.GO_REGISTER,
            "pb.RegisterXServer(server, impl) registers a value of type T "
            "(&T{}, new(T), or a name bound once to one of those) for service X, "
            "and T declares, in the same Go package directory, a method named "
            "as this rpc.",
        ),
        (
            grpc_contracts.GO_REGISTER_CONSTRUCTOR,
            "pb.RegisterXServer(server, f(...)) registers the value of f, a "
            "function of the same Go package directory (declared in one file "
            "only) whose only result is T or *T for a struct type T of that "
            "package, for service X; T declares, in that directory, a method "
            "named as this rpc.",
        ),
        (
            grpc_contracts.PYTHON_SERVICER,
            "A class inheriting XServicer (or a class of the same file "
            "inheriting such a class) defines a method named as this rpc of "
            "service X.",
        ),
        (
            grpc_contracts.JAVA_IMPL_BASE,
            "A class extending XGrpc.XImplBase declares a method named as this "
            "rpc of service X with a lowercase first letter.",
        ),
        (
            grpc_contracts.CSHARP_SERVICE_BASE,
            "A C# class deriving from X.XBase (the base class Grpc.Tools "
            "generates inside the static class X), or a class of the same "
            "file deriving from such a class, declares a method named as this "
            "rpc of service X.",
        ),
        (
            grpc_contracts.NODE_ADD_SERVICE,
            "server.addService(pkg.X.service, {key: handler}) maps the rpc "
            "named by key to a function or method declared in the same file.",
        ),
        (
            grpc_contracts.NODE_STATIC_ADD_SERVICE,
            "server.addService(m.XService, {key: handler}), where m is a "
            "grpc-tools *_grpc_pb module (or XService is imported from one), "
            "maps the rpc named by key to a function or method declared in the "
            "same file.",
        ),
    )
}
GRAPHQL_RESOLVER_RULES = {
    "nestjs-graphql": register_rule(
        EvidenceRule(
            "service-contracts.nestjs-graphql-resolver",
            "pattern_matched",
            "A method of a class decorated @Resolver carries @Query, @Mutation or "
            "@Subscription imported from @nestjs/graphql; the root field is the "
            "decorator's literal name, the name of its options, or the method's "
            "name, in the GraphQL server of the file's Node package.",
        )
    ),
    "resolver-map": register_rule(
        EvidenceRule(
            "service-contracts.graphql-resolver-map",
            "pattern_matched",
            "In a Node package that depends on a GraphQL server taking resolver "
            "maps (Apollo Server, graphql-tools, GraphQL Yoga, Mercurius), a "
            "member with a literal name of an object literal under a Query, "
            "Mutation or Subscription key resolves that root field.",
        )
    ),
}

GRAPHQL_DOCUMENT_RULE = register_rule(
    EvidenceRule(
        "service-contracts.repository-contains-graphql",
        "resolved",
        "The repository has this GraphQL document (*.graphql, *.gql) at this path.",
    )
)
GRAPHQL_CLIENT_RULE = register_rule(
    EvidenceRule(
        "service-contracts.graphql-client-operation",
        "pattern_matched",
        "An operation in a GraphQL template or .graphql file of a Node package "
        "that depends on a GraphQL client (Apollo Client, urql, graphql-request, "
        "Relay) selects a root field that exactly one GraphQL server of the "
        "repository resolves, or, when none resolves it, that exactly one "
        "declares.",
    )
)

GRPC_CALL_RULE = register_rule(
    EvidenceRule(
        grpc_contracts.STUB_CALL,
        "pattern_matched",
        "A method is called on a stub of service X built by its generated "
        "factory (NewXClient, XStub, XGrpc.newStub/newBlockingStub/"
        "newFutureStub, new pkg.X, or new m.XClient for a grpc-tools *_grpc_pb "
        "module m), directly or through a name bound in the "
        "same file only to stubs of X; in C#, a value of the generated type "
        "X.XClient (new, or a parameter, field, property, local or method "
        "result declared with it, looked up in its enclosing scope); X is "
        "declared by exactly one proto "
        "package and the method names one of its rpcs. The channel's target "
        "address is not read.",
    )
)


_BINDER_TYPES = {"rabbit": "amqp", "kafka": "kafka", "kstream": "kafka"}


def _build_unread(maven, module):
    """True when a module's build, or one it depends on, is not fully read:
    neither a pom.xml nor a Gradle file, or a Gradle project() not found."""
    if module not in maven.dependencies:
        return True
    pending, seen = [module], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in maven.unresolved:
            return True
        pending += [
            m
            for artifact in maven.dependencies.get(current, [])
            for m in maven.artifacts.get(artifact, [])
        ]
    return False


def _binder_name(single, binding):
    name = single(f"spring.cloud.stream.bindings.{binding}.binder") or single(
        "spring.cloud.stream.default-binder", "spring.cloud.stream.defaultBinder"
    )
    if name is None or "${" in name:
        return None
    return single(f"spring.cloud.stream.binders.{name}.type") or name


def _binders(config, marker, binding, binders):
    """[(broker, Compose services)] a binding uses in a service with
    ``binders`` (kafka, amqp): the only one; with several, the binder its
    ``spring.cloud.stream.bindings.<binding>.binder`` or the service's
    ``spring.cloud.stream.default-binder`` names, by
    ``spring.cloud.stream.binders.<name>.type`` or the standard names.

    Those keys are read in the default profile, or, for a module that
    Compose services build, in each service with its active profiles: one
    entry per broker, with the services that use it. A service whose binder
    is not read gives (None, [service])."""
    if len(binders) == 1:
        units = tuple(sorted(u.label for u in config.units(marker)))
        return [(next(iter(binders)), units)]
    keys = (
        "spring.cloud.stream.default-binder",
        "spring.cloud.stream.defaultBinder",
    )
    units = config.units(marker)
    if not units:

        def single(*keys):
            found = {
                v for k in keys for v in config.values(marker, k, default_profile=True)
            }
            return next(iter(found)) if len(found) == 1 else None

        kind = _binder_name(single, binding)
        broker = _BINDER_TYPES.get(kind)
        return [(broker if broker in binders else None, ())]
    per_unit = {}
    for key in (
        f"spring.cloud.stream.bindings.{binding}.binder",
        *keys,
    ):
        for label, value in config.deployment_values(marker, key).items():
            per_unit.setdefault(label, {})[key] = value
    grouped = {}
    for label in sorted(per_unit):
        values = per_unit[label]

        def single(*names, values=values, label=label):
            found = {values.get(n) for n in names} - {None}
            if not found and names[0].startswith("spring.cloud.stream.binders."):
                value = config.deployment_values(marker, names[0]).get(label)
                found = {value} - {None}
            return next(iter(found)) if len(found) == 1 else None

        kind = _binder_name(single, binding)
        broker = _BINDER_TYPES.get(kind)
        grouped.setdefault(broker if broker in binders else None, []).append(label)
    return [(broker, tuple(labels)) for broker, labels in grouped.items()]


def _stream_endpoints(root, config, sites, repo_url):
    """Message endpoints of Spring Cloud Stream binding sites, one per
    service that runs the code (see ``service_contracts.cloud_stream``)."""
    if not sites:
        return [], []
    maven = MavenModules(root)
    endpoints, unread = [], []
    placed = []
    for site in sites:
        module = _module_root(root, site.file)
        if module is None:
            if site.function is None:
                unread.append(
                    MessageUnread(site.file, site.line, site.role, "no_module")
                )
            continue
        module = module.as_posix()
        if config.service(site.file)[0] is not None:
            deploying = [module]
        else:
            deploying = [
                m
                for m in maven.dependents(module)
                if config.service(f"{m}/pom.xml")[0] is not None
            ]
        if not deploying and (
            site.function is None
            or maven.binders(module)
            or _build_unread(maven, module)
        ):
            # A functional bean is only a binding in a stream application.
            unread.append(MessageUnread(site.file, site.line, site.role, "no_service"))
        placed.append((site, module, deploying))
    # The functional beans each service runs, for its function definition.
    functions = {}
    for site, _, deploying in placed:
        if site.function is not None:
            for current in deploying:
                functions.setdefault(current, set()).add(site.function)
    for site, module, deploying in placed:
        for current in deploying:
            marker = f"{current}/pom.xml"
            service = config.service(marker)[0]
            binders = maven.binders(current)
            if not binders:
                # A functional bean outside a stream application is no binding,
                # unless the build that would say so is not read.
                if site.function is None:
                    reason = "binder_unknown"
                elif _build_unread(maven, current):
                    reason = "build_not_read"
                else:
                    continue
                unread.append(MessageUnread(site.file, site.line, site.role, reason))
                continue
            binding = site.binding
            if site.function is not None:
                # spring.cloud.stream.function.definition is its older name.
                definition = sorted(
                    set(config.values(marker, "spring.cloud.function.definition"))
                    | set(
                        config.values(marker, "spring.cloud.stream.function.definition")
                    )
                )
                reason = None
                if len(definition) > 1 or any("${" in d for d in definition):
                    reason = "function_definition_not_literal"
                elif definition:
                    names = [n.strip() for n in re.split(r"[;,]", definition[0])]
                    if any("|" in n and site.function in n.split("|") for n in names):
                        reason = "function_composed"
                    elif site.function not in names:
                        reason = "function_not_bound"
                elif functions.get(current) != {site.function}:
                    # Without a definition, only a service's single
                    # functional bean is bound.
                    reason = "function_not_bound"
                renamed = config.values(
                    marker, f"spring.cloud.stream.function.bindings.{binding}"
                )
                if reason is None and renamed:
                    if len(renamed) == 1 and "${" not in renamed[0]:
                        binding = renamed[0]
                    else:
                        reason = "binding_not_literal"
                if reason is not None:
                    unread.append(
                        MessageUnread(site.file, site.line, site.role, reason)
                    )
                    continue
            used = _binders(config, marker, binding, binders)
            if any(broker is None for broker, _ in used):
                unread.append(
                    MessageUnread(site.file, site.line, site.role, "binder_unknown")
                )
            used = [(b, labels) for b, labels in used if b is not None]
            if not used:
                continue
            configured = config.values(
                marker, f"spring.cloud.stream.bindings.{binding}.destination"
            )
            if not configured:
                destination = binding
            elif len(configured) == 1 and "${" not in configured[0]:
                destination = configured[0]
            else:
                unread.append(
                    MessageUnread(
                        site.file, site.line, site.role, "destination_not_literal"
                    )
                )
                continue
            identity = config.identity(marker)[0]
            for binder, labels in used:
                key = f"{site.role} {service} {site.binding} {site.line}"
                endpoints.append(
                    MessageEndpoint(
                        id=identifier(
                            repo_url,
                            site.file,
                            key if len(used) == 1 else f"{key} {binder}",
                            "MessageEndpoint",
                            0,
                        ),
                        file=site.file,
                        line=site.line,
                        column=site.column,
                        declaration_id=site.declaration_id,
                        role=site.role,
                        broker=binder,
                        destination=(
                            destination + "/#" if binder == "amqp" else destination
                        ),
                        client=(
                            "spring-cloud-stream"
                            if site.function is None
                            else "spring-cloud-function"
                        ),
                        module=None if current == module else current,
                        service=None if current == module else service,
                        service_identity=identity,
                        deployed_as=labels,
                    )
                )
    return endpoints, unread


def _deployed(root, deployments, routes, requests):
    """Routes named after the one service the deployments running their
    file give, when their language gives none; Locust requests written
    without a host given the host of their deployments' ``--host``."""
    from dataclasses import replace

    routes = [
        r if r.service is not None else replace(r, service=deployments.service(r.file))
        for r in routes
    ]
    hosts = {}
    placed = []
    for request in requests:
        if request.client == "locust" and request.url.startswith("/"):
            if request.file not in hosts:
                units = deployments.running(request.file)
                found = {}
                for unit in units:
                    found.setdefault(
                        locust_host(command_line(root, unit), unit), []
                    ).append(unit.label)
                hosts[request.file] = (
                    next(iter(found.items()))
                    if len(found) == 1 and None not in found
                    else None
                )
            given = hosts[request.file]
            if given is not None:
                host, labels = given
                request = replace(
                    request,
                    url=host.rstrip("/") + request.url,
                    host_from=tuple(sorted(labels)),
                )
        placed.append(request)
    return routes, placed


def _gateways(root, config, sources):
    """Gateway routes: Spring Cloud Gateway routes in the configuration of
    the analyzed Java files' modules, nginx proxy locations in ``*.conf``
    and ``*.conf.template`` files outside hidden and dependency folders, and
    YARP and Ocelot routes in the JSON configuration of analyzed C#
    projects."""
    routes, unread, seen = [], [], set()
    for coverage, _, _ in sources:
        if coverage.language != "java":
            continue
        for path in config.files(coverage.file):
            if path in seen or path.suffix not in (".yml", ".yaml"):
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "gateway" not in text:
                continue
            found = spring_gateway_routes(path, text, path.relative_to(root).as_posix())
            routes.extend(found[0])
            unread.extend(found[1])
    for path in sorted(root.rglob("*.conf*")):
        relative = path.relative_to(root)
        if not path.is_file() or not (
            path.name.endswith(".conf") or path.name.endswith(".conf.template")
        ):
            continue
        if any(
            part.startswith(".") or part in ("node_modules", "vendor")
            for part in relative.parts[:-1]
        ) or _is_test_source(relative.as_posix()):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "proxy_pass" not in text:
            continue
        found = nginx_routes(text, relative.as_posix())
        routes.extend(found[0])
        unread.extend(found[1])
    # .NET gateways: YARP's ReverseProxy section and Ocelot routes in the
    # base JSON configuration of a project with analyzed C# sources. An
    # environment file (appsettings.Development.json, ocelot.Docker.json)
    # beside its base file (appsettings.json, ocelot.json) is an overlay for
    # one environment and is not read, as Spring profile documents are not.
    projects = {
        PurePosixPath(c.file).parent for c, _, _ in sources if c.language == "csharp"
    }
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(
            f
            for f in folders
            if not f.startswith(".")
            and f not in ("node_modules", "vendor", "bin", "obj")
        )
        here = PurePosixPath(directory).relative_to(PurePosixPath(root))
        if not any(here in (p, *p.parents) for p in projects):
            continue
        names = {f for f in files if f.lower().endswith(".json")}
        for name in sorted(names):
            stem = name[: -len(".json")]
            if "." in stem and stem.rpartition(".")[0] + ".json" in names:
                continue
            try:
                text = (root / here / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            relative = (here / name).as_posix()
            if name == "appsettings.json" and "ReverseProxy" in text:
                found = yarp_routes(text, relative)
            elif "UpstreamPathTemplate" in text:
                found = ocelot_routes(text, relative)
            else:
                continue
            routes.extend(found[0])
            unread.extend(found[1])
    return routes, unread


def _is_test_source(path):
    """A file of a Maven or Gradle test source set (``src/test/``), or a test
    file by its language's naming convention."""
    name = path.rpartition("/")[2]
    return (
        path.startswith("src/test/")
        or "/src/test/" in path
        or name.endswith("_test.go")
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
        or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name) is not None
    )


def _config_paths(root, names):
    """Repository paths of files with one of ``names``, outside hidden,
    ``node_modules`` and ``vendor`` folders."""
    found = []
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(
            f
            for f in folders
            if not f.startswith(".") and f not in ("node_modules", "vendor")
        )
        for name in sorted(files):
            if names(name) if callable(names) else name in names:
                found.append(
                    (PurePosixPath(directory) / name).relative_to(root).as_posix()
                )
    return found


def _script_constants(root, sources):
    """String constants the repository's scripts export, import or have
    defined by angular.json, resolving relative imports and tsconfig.json
    ``paths`` aliases to analyzed script files."""
    scripts = {
        coverage.file: (tree, content, coverage.language)
        for coverage, content, tree in sources
        if coverage.language in ("javascript", "typescript")
    }
    if not scripts:
        return None
    files = set(scripts)
    ts_paths, defines = {}, []
    for path in _config_paths(root, ("tsconfig.json", "angular.json")):
        try:
            content = (root / path).read_bytes()
        except OSError:
            continue
        if path.endswith("angular.json"):
            defines.extend(angular_defines(path, content))
            continue
        found = read_tsconfig_paths(path, content)
        if found is not None:
            ts_paths[path.rpartition("/")[0]] = found
    cache = {}

    def resolve(file, module):
        key = (file, module)
        if key not in cache:
            cache[key] = resolve_script_module(
                file, module, files
            ) or resolve_script_alias(file, module, files, ts_paths)
        return cache[key]

    return ScriptConstants(scripts, resolve, defines)


def _node_package(root, file):
    """The nearest directory above ``file`` with a package.json, or None."""
    path = PurePosixPath(file).parent
    while True:
        if (root / path / "package.json").is_file():
            return path.as_posix()
        if path == path.parent:
            return None
        path = path.parent


def _package_dependencies(root, package):
    """Names of the dependencies a package.json lists, of every kind."""
    try:
        manifest = json.loads(
            (root / (package or ".") / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(manifest, dict):
        return frozenset()
    names = set()
    for kind in ("dependencies", "devDependencies", "peerDependencies"):
        found = manifest.get(kind)
        if isinstance(found, dict):
            names.update(found)
    return frozenset(names)


_MODULE_SUFFIXES = ("", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx")


def _follow_resolver_maps(root, sources, references, repo_url, definitions):
    """Root fields of the modules that resolver map keys name (``{Query}``
    bound to ``require('./query')`` or an import of it). Only relative
    modules found in the repository are followed; any other is unread."""
    by_file = {
        coverage.file: (coverage, content, tree) for coverage, content, tree in sources
    }
    resolvers, unread = [], []
    for reference in references:
        target = None
        if reference.module.startswith("."):
            base = PurePosixPath(reference.file).parent / reference.module
            candidates = [f"{base}{suffix}" for suffix in _MODULE_SUFFIXES] + [
                f"{base}/index{suffix}" for suffix in _MODULE_SUFFIXES[1:]
            ]
            for candidate in candidates:
                normalized = posixpath.normpath(candidate)
                if normalized in by_file:
                    target = by_file[normalized]
                    break
        if target is None:
            unread.append(
                Unread(
                    reference.file,
                    reference.line,
                    "graphql",
                    "resolver_map_not_literal",
                )
            )
            continue
        coverage, content, tree = target
        found = follow_reference(
            reference,
            tree,
            content,
            repo_url,
            coverage.file,
            definitions,
            coverage.language,
        )
        resolvers.extend(found[0])
        unread.extend(found[1])
    return resolvers, unread


def _python_mounts(sources):
    """{file: {router name: full prefixes}} of Python routers mounted in
    other routers (``include_router``, ``register_blueprint``)."""
    found = {
        coverage.file: python_routers(tree, content)
        for coverage, content, tree in sources
        if coverage.language == "python"
    }
    mounts = {}
    if any(m for _, m, _ in found.values()):
        for (file, name), prefixes in compose_python_prefixes(found).items():
            mounts.setdefault(file, {})[name] = prefixes
    return mounts


def _express_mounts(sources):
    """{file: {router name: full prefixes}} of Express routers mounted with
    ``use`` in other routers or apps."""
    found = {
        coverage.file: express_routers(tree, content)
        for coverage, content, tree in sources
        if coverage.language in ("javascript", "typescript")
        and (b"express" in content or b".use(" in content)
    }
    if not any(facts[2] for facts in found.values()):
        return {}
    files = set(found)
    mounts = {}
    for (file, name), prefixes in compose_express_prefixes(
        found, lambda f, m: resolve_script_module(f, m, files)
    ).items():
        mounts.setdefault(file, {})[name] = prefixes
    return mounts


def _nest_applications(root, sources):
    """(global prefix, unread reason) of each Node package's NestJS
    application, from the setGlobalPrefix, enableVersioning and RouterModule
    calls of its TypeScript files: one literal prefix applies to every route
    of the package; anything else makes its routes unread."""
    settings = {}
    for coverage, content, tree in sources:
        if coverage.language != "typescript" or not any(
            needle in content
            for needle in (b"setGlobalPrefix", b"enableVersioning", b"RouterModule")
        ):
            continue
        package = _node_package(root, coverage.file)
        settings.setdefault(package, []).extend(
            nest_settings(tree, content, coverage.language)
        )
    found = {}
    for package, facts in settings.items():
        reasons = sorted({v for k, v in facts if k == "unread"})
        prefixes = {v for k, v in facts if k == "prefix"}
        if reasons:
            found[package] = (None, reasons[0])
        elif len(prefixes) > 1:
            found[package] = (None, "global_prefix_ambiguous")
        else:
            found[package] = (next(iter(prefixes), None), None)
    return found


def _go_packages(definitions):
    """Go directory -> (functions, methods) by name, for handler anchoring."""
    packages = {}
    for d in definitions:
        if not d.file.endswith(".go") or d.kind not in ("Function", "Method"):
            continue
        functions, methods = packages.setdefault(d.file.rpartition("/")[0], ({}, {}))
        if d.kind == "Function":
            functions.setdefault(d.symbol, []).append(d.id)
        else:
            methods.setdefault(d.symbol.rpartition(".")[2], []).append(d.id)
    return packages


def _evidence(rule):
    return {"evidence_type": rule.evidence_type, "rule_id": rule.rule_id}


@register_tool
class ServiceContractTool(AnalysisTool):
    def __init__(self, repo_path):
        super().__init__(repo_path)
        self.last_facts = None
        self.last_grpc = None

    def _get_metadata(self):
        return ToolMetadata(
            name="service-contracts",
            version=__version__,
            capabilities={ToolCapability.DEPENDENCY_ANALYSIS},
            supported_languages=set(SUPPORTED_LANGUAGES),
            requires_binary=False,
            binary_name=None,
            performance_class="unmeasured",
        )

    async def check_availability(self):
        return importlib.util.find_spec("tree_sitter") is not None and any(
            importlib.util.find_spec(f"tree_sitter_{name}") is not None
            for name in ("go", "java", "javascript", "python", "typescript")
        )

    async def analyze(
        self, repo_url, languages=None, file_patterns=None, skip_patterns=None
    ):
        started = time.monotonic()
        selected = tuple(
            sorted(
                set(SUPPORTED_LANGUAGES)
                & set(SUPPORTED_LANGUAGES if languages is None else languages)
            )
        )
        if not selected:
            raise ValueError(
                "service-contracts reads " + ", ".join(SUPPORTED_LANGUAGES) + " only"
            )
        result = analyze_repository(
            self.repo_path,
            repository_id=repo_url,
            languages=selected,
            include=("*",) if file_patterns is None else tuple(file_patterns),
            exclude=() if skip_patterns is None else tuple(skip_patterns),
        )
        root = self.repo_path.resolve()
        routes, requests, unread = [], [], []
        endpoints, messaging_unread, stream_sites = [], [], []
        graphql_resolvers = []
        graphql_declared = []
        resolver_map_packages = {}
        resolver_references = []
        partial_scripts = []
        graphql_requests = []
        graphql_documents = {}
        client_packages = {}
        server_packages = {}
        changed = []
        config = SpringConfig(root)
        parsers = {}
        packages = _go_packages(result.definitions)
        test_files = 0
        grpc_sources = []
        sources = []
        for coverage in result.coverage:
            if coverage.status != "analyzed":
                if (
                    coverage.status == "partial"
                    and coverage.language in ("javascript", "typescript")
                    and not _is_test_source(coverage.file)
                ):
                    partial_scripts.append(coverage.file)
                continue
            if _is_test_source(coverage.file):
                test_files += 1
                continue
            try:
                content = (root / coverage.file).read_bytes()
            except OSError:
                changed.append(coverage.file)
                continue
            if sha256(content).hexdigest() != coverage.digest:
                changed.append(coverage.file)
                continue
            grpc_sources.append((coverage.file, coverage.language, content))
            suffix = "." + coverage.file.rsplit(".", 1)[-1]
            key = (coverage.language, suffix if suffix == ".tsx" else "")
            if key not in parsers:
                parsers[key] = make_parser(coverage.language, suffix)
            sources.append((coverage, content, parsers[key].parse(content)))
        # Java constants other classes name (Queues.NAME), by qualified class;
        # a class declared in two files gives none.
        constants, seen = {}, set()
        for coverage, content, tree in sources:
            if coverage.language != "java":
                continue
            for qualified, fields in java_constants(tree, content).items():
                if qualified in seen:
                    constants.pop(qualified, None)
                else:
                    seen.add(qualified)
                    constants[qualified] = fields
        # Arguments of every `new` of a Java class, for the fields its
        # constructors set from their parameters.
        constructions = {}
        for coverage, content, tree in sources:
            if coverage.language == "java" and b"new " in content:
                for qualified, calls in java_constructions(
                    tree, content, constants
                ).items():
                    constructions.setdefault(qualified, []).extend(calls)
        # Base addresses of C# typed HttpClients.
        csharp = [
            (coverage.file, tree, content)
            for coverage, content, tree in sources
            if coverage.language == "csharp"
        ]
        csharp_usings = global_usings(
            root, (c for c in csharp if b"global using" in c[2])
        )
        client_bases = typed_client_bases(
            typed_client_registrations(
                c
                for c in csharp
                if re.search(rb"AddHttpClient|AddRefitClient|RestService", c[2])
            ),
            [
                d.symbol
                for d in result.definitions
                if d.kind in ("Class", "Struct", "Record", "Interface")
                and d.file.endswith(".cs")
            ],
            csharp_usings,
        )
        # Spring interfaces openapi-generator makes from specs at build time.
        # Java API classes it makes for clients, and the modules that set a
        # client's base URL at run time (ApiClient.setBasePath / setServers).
        generated, generated_clients, openapi_unread = (
            generated_apis(root)
            if any(c.language == "java" for c, _, _ in sources)
            else ({}, {}, [])
        )
        base_set = {
            _module_root(root, coverage.file)
            for coverage, content, _ in sources
            if coverage.language == "java"
            and generated_clients
            and re.search(rb"\.(setBasePath|setServers|setServerIndex)\(", content)
        }
        # A module whose Java code calls setExchange has no fixed template
        # exchange; otherwise it is spring.rabbitmq.template.exchange, or the
        # default exchange "" when that key is not set.
        exchange_set = {
            _module_root(root, coverage.file)
            for coverage, content, _ in sources
            if coverage.language == "java" and b"setExchange(" in content
        }

        def template_exchange(file):
            module = _module_root(root, file)
            if module is None or module in exchange_set:
                return None
            configured = config.values(file, "spring.rabbitmq.template.exchange")
            if not configured:
                return ""
            if len(configured) == 1 and "${" not in configured[0]:
                return configured[0]
            return None

        # Each module's STOMP application destination prefixes: none ("")
        # when no file sets them, unknown (None) when they are not literal or
        # set in more than one place.
        prefix_lists = {}
        for coverage, content, tree in sources:
            if (
                coverage.language == "java"
                and b"setApplicationDestinationPrefixes" in content
            ):
                found = stomp_prefixes(tree, content, constants)
                if found is not None:
                    prefix_lists.setdefault(
                        _module_root(root, coverage.file), []
                    ).append(found)

        def application_prefixes(file):
            lists = prefix_lists.get(_module_root(root, file))
            if not lists:
                return [""]
            if len(lists) > 1 or any(PLACEHOLDER in p for p in lists[0]):
                return None
            return lists[0]

        nest = _nest_applications(root, sources)
        python_mounts = _python_mounts(sources)
        python_mounts.update(_express_mounts(sources))
        scripts = _script_constants(root, sources)
        calls = ScriptCalls(scripts) if scripts is not None else None
        for coverage, content, tree in sources:
            if coverage.language == "csharp":
                routes_found, _, unread_found = extract_aspnet(
                    tree, content, repo_url, coverage.file, result.definitions
                )
                found = (
                    routes_found,
                    extract_csharp_requests(
                        tree,
                        content,
                        repo_url,
                        coverage.file,
                        result.definitions,
                        client_bases,
                    )
                    + (
                        extract_refit_requests(
                            tree,
                            content,
                            repo_url,
                            coverage.file,
                            result.definitions,
                            client_bases,
                            csharp_usings,
                        )
                        if b"interface" in content
                        else []
                    ),
                    unread_found,
                )
            elif coverage.language == "php":
                found = extract_php(
                    tree, content, repo_url, coverage.file, result.definitions
                )
            elif coverage.language == "java":
                service, context_path = config.service(coverage.file)
                found = extract_spring(
                    tree,
                    content,
                    repo_url,
                    coverage.file,
                    result.definitions,
                    service=service,
                    context_path=context_path,
                    constants=constants,
                    generated=generated,
                    clients=generated_clients,
                    client_base_fixed=_module_root(root, coverage.file) not in base_set,
                    constructions=constructions,
                )
            else:
                found = extract_web(
                    tree,
                    content,
                    repo_url,
                    coverage.file,
                    result.definitions,
                    coverage.language,
                    package=packages.get(coverage.file.rpartition("/")[0]),
                    nest=nest.get(_node_package(root, coverage.file)),
                    mounts=python_mounts.get(coverage.file),
                    externals=(
                        scripts.externals(coverage.file)
                        if scripts is not None
                        else None
                    ),
                    calls_of=(
                        functools.partial(calls.calls, coverage.file)
                        if calls is not None and coverage.file in scripts.sources
                        else None
                    ),
                )
            routes.extend(found[0])
            requests.extend(found[1])
            unread.extend(found[2])
            if coverage.language in (
                "go",
                "java",
                "python",
                "javascript",
                "typescript",
            ):
                found = extract_messaging(
                    tree,
                    content,
                    repo_url,
                    coverage.file,
                    result.definitions,
                    coverage.language,
                    constants=constants,
                    template_exchange=(
                        template_exchange(coverage.file)
                        if coverage.language == "java"
                        else None
                    ),
                    application_prefixes=(
                        application_prefixes(coverage.file)
                        if coverage.language == "java"
                        else None
                    ),
                    calls_of=(
                        functools.partial(calls.calls, coverage.file)
                        if calls is not None and coverage.file in scripts.sources
                        else None
                    ),
                )
                endpoints.extend(found[0])
                messaging_unread.extend(found[1])
            if coverage.language == "typescript" and b"@nestjs/graphql" in content:
                found = extract_nest_resolvers(
                    tree,
                    content,
                    repo_url,
                    coverage.file,
                    result.definitions,
                    coverage.language,
                )
                graphql_resolvers.extend(found[0])
                unread.extend(found[1])
            if coverage.language in ("javascript", "typescript") and any(
                needle in content for needle in (b"Query", b"Mutation", b"Subscription")
            ):
                package = _node_package(root, coverage.file)
                if package not in resolver_map_packages:
                    resolver_map_packages[package] = bool(
                        _package_dependencies(root, package) & RESOLVER_MAP_SERVERS
                    )
                if resolver_map_packages[package]:
                    found = extract_resolver_maps(
                        tree,
                        content,
                        repo_url,
                        coverage.file,
                        result.definitions,
                        coverage.language,
                    )
                    graphql_resolvers.extend(found[0])
                    unread.extend(found[1])
                    resolver_references.extend(found[2])
                    found = template_schemas(tree, content, coverage.file)
                    graphql_declared.extend(found[0])
                    unread.extend(found[1])
            if coverage.language in ("javascript", "typescript") and any(
                needle in content for needle in (b"gql", b"graphql", b"GraphQL")
            ):
                package = _node_package(root, coverage.file)
                if package not in client_packages:
                    client_packages[package] = bool(
                        _package_dependencies(root, package) & GRAPHQL_CLIENTS
                    )
                if client_packages[package]:
                    found = client_operations(
                        tree,
                        content,
                        repo_url,
                        coverage.file,
                        result.definitions,
                        coverage.language,
                    )
                    graphql_requests.extend(found[0])
                    unread.extend(found[1])
            if coverage.language == "java" and (
                b"cloud.stream" in content
                or (b"java.util.function" in content and b"@Bean" in content)
            ):
                found = extract_stream(
                    tree, content, coverage.file, result.definitions, constants
                )
                stream_sites.extend(found[0])
                messaging_unread.extend(found[1])
        for path in sorted(
            p for suffix in ("*.graphql", "*.gql") for p in root.rglob(suffix)
        ):
            relative = path.relative_to(root)
            if any(
                part.startswith(".") or part in ("node_modules", "vendor")
                for part in relative.parts
            ):
                continue
            raw = path.read_bytes()
            source = raw.decode("utf-8", errors="replace")
            graphql_documents[relative.as_posix()] = sha256(raw).hexdigest()
            found = schema_fields(source, relative.as_posix())
            graphql_declared.extend(found[0])
            unread.extend(found[1])
            package = _node_package(root, relative.as_posix())
            if package not in client_packages:
                client_packages[package] = bool(
                    _package_dependencies(root, package) & GRAPHQL_CLIENTS
                )
            if client_packages[package]:
                found = operation_fields(source, relative.as_posix())
                graphql_requests.extend(found[0])
                unread.extend(found[1])
        # A script the grammar could not fully parse is not read; in a package
        # with resolver maps it may hold some, so its fields are unknown.
        for file in partial_scripts:
            package = _node_package(root, file)
            if package not in resolver_map_packages:
                resolver_map_packages[package] = bool(
                    _package_dependencies(root, package) & RESOLVER_MAP_SERVERS
                )
            try:
                content = (root / file).read_bytes()
            except OSError:
                continue
            if resolver_map_packages[package] and any(
                key in content for key in (b"Query", b"Mutation", b"Subscription")
            ):
                unread.append(Unread(file, 1, "graphql", "source_not_parsed"))
        found = _follow_resolver_maps(
            root, sources, resolver_references, repo_url, result.definitions
        )
        graphql_resolvers.extend(found[0])
        unread.extend(found[1])
        found = _stream_endpoints(root, config, stream_sites, repo_url)
        endpoints.extend(found[0])
        messaging_unread.extend(found[1])
        if changed:
            raise ValueError(
                "Files changed during analysis: " + ", ".join(sorted(changed))
            )
        gateways, gateway_unread = _gateways(root, config, sources)
        deployments = Deployments(deployed_units(root))
        routes, requests = _deployed(root, deployments, routes, requests)
        resolutions = contract_rules.resolve_requests(
            routes, requests, gateways, deployments.hosts()
        )
        self.last_facts = (result, routes, requests, unread, resolutions)
        grpc = grpc_contracts.collect(
            root,
            grpc_sources,
            result.definitions,
            repo_url,
            grpc_contracts.proto_files(
                root,
                ("*",) if file_patterns is None else tuple(file_patterns),
                () if skip_patterns is None else tuple(skip_patterns),
            ),
        )
        self.last_grpc = grpc
        self.last_messages = (endpoints, messaging_unread)

        definitions = {d.id: d for d in result.definitions}
        languages_by_file = {c.file: c for c in result.coverage}
        files, symbols, nodes, relationships = {}, {}, [], []

        def file_node(path):
            if path not in files and path in graphql_documents:
                # A GraphQL document no language tool reads: the repository
                # contains it.
                qualified = f"{repo_url}::{path}::file"
                files[path] = NodeData(
                    ["File"],
                    {
                        "qualified_name": qualified,
                        "repo_url": repo_url,
                        "path": path,
                        "file_path": path,
                        "name": path,
                        "language": "graphql",
                        "file_sha256": graphql_documents[path],
                    },
                )
                relationships.append(
                    RelationshipData(
                        "CONTAINS",
                        ["Repository"],
                        {
                            "qualified_name": f"{repo_url}::repository",
                            "repo_url": repo_url,
                        },
                        ["File"],
                        {"qualified_name": qualified, "repo_url": repo_url},
                        _evidence(GRAPHQL_DOCUMENT_RULE),
                    )
                )
            if path not in files:
                coverage = languages_by_file[path]
                files[path] = NodeData(
                    ["File"],
                    {
                        "qualified_name": f"{repo_url}::{path}::file",
                        "repo_url": repo_url,
                        "path": path,
                        "file_path": path,
                        "name": path,
                        "language": coverage.language,
                        "coverage_status": coverage.status,
                        "file_sha256": coverage.digest,
                    },
                )
            return ["File"], files[path].properties["qualified_name"]

        def symbol_node(definition_id):
            d = definitions[definition_id]
            name = f"{repo_url}::{d.file}::{d.symbol}::{d.id}"
            if definition_id not in symbols:
                symbols[definition_id] = NodeData(
                    [d.kind, "Symbol"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "definition_id": d.id,
                        "file_path": d.file,
                        "name": d.symbol,
                        "display_name": d.symbol,
                        "kind": d.kind,
                        "start_line": d.line,
                        "end_line": d.end_line,
                        "column": d.column,
                        "end_column": d.end_column,
                        "coordinate_encoding": d.coordinate_encoding,
                    },
                )
            return [d.kind, "Symbol"], name

        route_names = {}
        for route in routes:
            name = f"{repo_url}::{route.file}::http-route::{route.id}"
            route_names[route.id] = name
            nodes.append(
                NodeData(
                    ["HttpRoute", "Contract"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "route_id": route.id,
                        "protocol": "http",
                        "name": f"{' '.join(route.methods)} {route.path}",
                        "display_name": f"{' '.join(route.methods)} {route.path}",
                        "methods": list(route.methods),
                        "path": route.path,
                        "framework": route.framework,
                        "file_path": route.file,
                        "line": route.line,
                        **({"service": route.service} if route.service else {}),
                        **({"base_unknown": True} if route.base_unknown else {}),
                        **(
                            {"declared_in": route.declared_in}
                            if route.declared_in
                            else {}
                        ),
                    },
                )
            )
            if route.handler_id is None:
                labels, handler = file_node(route.file)
            else:
                labels, handler = symbol_node(route.handler_id)
            relationships.append(
                RelationshipData(
                    "HANDLES",
                    labels,
                    {"qualified_name": handler, "repo_url": repo_url},
                    ["HttpRoute", "Contract"],
                    {"qualified_name": name, "repo_url": repo_url},
                    {
                        "route_id": route.id,
                        "line": route.line,
                        "column": route.column,
                        **_evidence(ROUTE_RULES[route.framework]),
                    },
                )
            )
        by_id = {r.id: r for r in requests}
        counts = {}
        undisclosed = {}
        for resolution in resolutions:
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            counts[key] = counts.get(key, 0) + 1
            request = by_id[resolution.request_id]
            written = (
                (request.method or "?")
                + " "
                + (request.url.replace(PLACEHOLDER, "{?}"))
            )
            if resolution.status != "linked":
                undisclosed.setdefault(request.file, {}).setdefault(
                    resolution.status + "_http_requests", set()
                ).add(written)
                continue
            if request.caller_id is None:
                source = file_node(request.file)
            else:
                source = symbol_node(request.caller_id)
            relationships.append(
                RelationshipData(
                    "REQUESTS",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    ["HttpRoute", "Contract"],
                    {
                        "qualified_name": route_names[resolution.route_id],
                        "repo_url": repo_url,
                    },
                    {
                        "request_id": request.id,
                        "client": request.client,
                        "request": written,
                        "line": request.line,
                        "column": request.column,
                        **(
                            {"url_alternatives": request.alternatives}
                            if request.alternatives > 1
                            else {}
                        ),
                        **(
                            {"argument_from": list(request.argument_from)}
                            if request.argument_from
                            else {}
                        ),
                        **(
                            {"target_service": resolution.target_service}
                            if resolution.target_service
                            else {}
                        ),
                        **(
                            {"gateway": resolution.gateway}
                            if resolution.gateway
                            else {}
                        ),
                        **(
                            {"declared_in": request.declared_in}
                            if request.declared_in
                            else {}
                        ),
                        **(
                            {"host_from": list(request.host_from)}
                            if request.host_from
                            else {}
                        ),
                        **_evidence(
                            (
                                LOCUST_HOST_RULES
                                if request.host_from
                                else {
                                    "openapi-java": OPENAPI_REQUEST_RULES,
                                    "openapi-spring-cloud": OPENAPI_FEIGN_REQUEST_RULES,
                                }.get(request.client, REQUEST_RULES)
                            )[resolution.rule_id]
                        ),
                    },
                )
            )
        graphql_fields = {}
        for item in graphql_declared + graphql_resolvers:
            scope = _node_package(root, item.file) or "."
            graphql_fields.setdefault((scope, item.operation, item.field), [[], []])
            index = 0 if isinstance(item, GraphqlDeclaration) else 1
            graphql_fields[(scope, item.operation, item.field)][index].append(item)
        declaring_scopes = {
            scope
            for scope, _, _ in graphql_fields
            if any(graphql_fields[k][0] for k in graphql_fields if k[0] == scope)
        }
        # A package with a resolver map the tool cannot list may resolve any
        # field: its declared fields without a read resolver are unknown.
        opaque_scopes = {
            _node_package(root, u.file) or "."
            for u in unread
            if u.kind == "graphql"
            and u.reason
            in ("resolver_map_not_literal", "resolver_spread", "source_not_parsed")
        }
        graphql_unread = {}
        for (scope, operation, field), (declared, resolving) in sorted(
            graphql_fields.items()
        ):
            full = f"{operation}.{field}"
            name = f"{repo_url}::graphql::{scope}::{full}"
            first = (declared or resolving)[0]
            if not resolving:
                reason = (
                    "resolution_unknown"
                    if scope in opaque_scopes
                    else "declared_not_resolved"
                )
                graphql_unread[reason] = graphql_unread.get(reason, 0) + 1
            elif not declared and scope in declaring_scopes:
                graphql_unread["resolved_not_declared"] = (
                    graphql_unread.get("resolved_not_declared", 0) + 1
                )
            nodes.append(
                NodeData(
                    ["GraphqlField", "Contract"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "protocol": "graphql",
                        "name": full,
                        "display_name": full,
                        "operation": operation,
                        "field": field,
                        "scope": scope,
                        "file_path": first.file,
                        "line": first.line,
                        **(
                            {"declared_in": [f"{d.file}:{d.line}" for d in declared]}
                            if declared
                            else {}
                        ),
                    },
                )
            )
            for resolver in resolving:
                if resolver.handler_id is None:
                    labels, source = file_node(resolver.file)
                else:
                    labels, source = symbol_node(resolver.handler_id)
                relationships.append(
                    RelationshipData(
                        "HANDLES",
                        labels,
                        {"qualified_name": source, "repo_url": repo_url},
                        ["GraphqlField", "Contract"],
                        {"qualified_name": name, "repo_url": repo_url},
                        {
                            "line": resolver.line,
                            "column": resolver.column,
                            **_evidence(GRAPHQL_RESOLVER_RULES[resolver.framework]),
                        },
                    )
                )
        # Each root field a client selects, to a server of the group nearest
        # the client: the server packages whose common directory with the
        # client's package is deepest. In that group, the one server that
        # resolves the field, or else the one that declares it.
        server_scopes = {
            scope
            for (scope, _, _), (_, resolving) in graphql_fields.items()
            if resolving
        } | opaque_scopes
        for coverage, _, _ in sources:
            if coverage.language not in ("javascript", "typescript"):
                continue
            package = _node_package(root, coverage.file)
            if package not in server_packages:
                server_packages[package] = bool(
                    _package_dependencies(root, package) & GRAPHQL_SERVERS
                )
            if server_packages[package]:
                server_scopes.add(package or ".")
        resolving_scopes, declaring_scopes_by_field = {}, {}
        for (scope, operation, field), (declared, resolving) in graphql_fields.items():
            if resolving:
                resolving_scopes.setdefault((operation, field), set()).add(scope)
            elif declared:
                declaring_scopes_by_field.setdefault((operation, field), set()).add(
                    scope
                )

        def nearest(package):
            parts = PurePosixPath(package or ".").parts
            depth = {
                scope: len(
                    list(
                        itertools.takewhile(
                            lambda pair: pair[0] == pair[1],
                            zip(parts, PurePosixPath(scope).parts),
                        )
                    )
                )
                for scope in server_scopes
            }
            deepest = max(depth.values(), default=0)
            return {scope for scope, d in depth.items() if d == deepest}

        groups = {}
        for request in graphql_requests:
            package = _node_package(root, request.file)
            if package not in groups:
                groups[package] = nearest(package)
            group = groups[package]
            key = (request.operation, request.field)
            scopes = sorted(resolving_scopes.get(key, set()) & group) or sorted(
                declaring_scopes_by_field.get(key, set()) & group
            )
            if len(scopes) != 1:
                reason = "client_field_ambiguous" if scopes else "client_field_unknown"
                graphql_unread[reason] = graphql_unread.get(reason, 0) + 1
                continue
            full = f"{request.operation}.{request.field}"
            if request.caller_id is None:
                labels, source = file_node(request.file)
            else:
                labels, source = symbol_node(request.caller_id)
            relationships.append(
                RelationshipData(
                    "REQUESTS",
                    labels,
                    {"qualified_name": source, "repo_url": repo_url},
                    ["GraphqlField", "Contract"],
                    {
                        "qualified_name": f"{repo_url}::graphql::{scopes[0]}::{full}",
                        "repo_url": repo_url,
                    },
                    {
                        "request": full,
                        **({"operation_name": request.name} if request.name else {}),
                        "line": request.line,
                        "column": request.column,
                        **_evidence(GRAPHQL_CLIENT_RULE),
                    },
                )
            )
        grpc_names = {}
        for (service, method), declared in sorted(grpc.index.methods.items()):
            full = f"{service}/{method}"
            name = f"{repo_url}::grpc::{full}"
            grpc_names[(service, method)] = name
            nodes.append(
                NodeData(
                    ["GrpcMethod", "Contract"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "protocol": "grpc",
                        "name": full,
                        "display_name": full,
                        "service": service,
                        "method": method,
                        "file_path": declared[0].file,
                        "line": declared[0].line,
                        "declared_in": [f"{m.file}:{m.line}" for m in declared],
                        # Only when every copy of the proto agrees.
                        **(
                            {"streaming": declared[0].streaming}
                            if len({m.streaming for m in declared}) == 1
                            and declared[0].streaming
                            else {}
                        ),
                    },
                )
            )
        for handler in grpc.handlers:
            labels, source = symbol_node(handler.handler_id)
            relationships.append(
                RelationshipData(
                    "HANDLES",
                    labels,
                    {"qualified_name": source, "repo_url": repo_url},
                    ["GrpcMethod", "Contract"],
                    {
                        "qualified_name": grpc_names[(handler.service, handler.method)],
                        "repo_url": repo_url,
                    },
                    {
                        "line": handler.line,
                        **_evidence(GRPC_SERVER_RULES[handler.rule]),
                    },
                )
            )
        grpc_counts = {}
        grpc_calls = {c.id: c for c in grpc.calls}
        for resolution in grpc.resolutions:
            key = resolution.status + (
                ":" + resolution.reason if resolution.reason else ""
            )
            grpc_counts[key] = grpc_counts.get(key, 0) + 1
            call = grpc_calls[resolution.call_id]
            written = f"{call.service}.{call.method}"
            if resolution.status != "linked":
                undisclosed.setdefault(call.file, {}).setdefault(
                    resolution.status + "_grpc_calls", set()
                ).add(written)
                continue
            if call.caller_id is None:
                source = file_node(call.file)
            else:
                source = symbol_node(call.caller_id)
            relationships.append(
                RelationshipData(
                    "REQUESTS",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    ["GrpcMethod", "Contract"],
                    {
                        "qualified_name": grpc_names[
                            (resolution.service, resolution.method)
                        ],
                        "repo_url": repo_url,
                    },
                    {
                        "request_id": call.id,
                        "request": written,
                        "line": call.line,
                        "column": call.column,
                        **_evidence(GRPC_CALL_RULE),
                    },
                )
            )
        for path, values in undisclosed.items():
            file_node(path)
            files[path].properties.update(
                {key: sorted(items) for key, items in sorted(values.items())}
            )
        topics = {}
        for endpoint in endpoints:
            name = (
                f"{repo_url}::{endpoint.broker}::message-destination::"
                f"{endpoint.destination}"
            )
            topics.setdefault(
                name,
                NodeData(
                    ["MessageDestination", "Contract"],
                    {
                        "qualified_name": name,
                        "repo_url": repo_url,
                        "protocol": endpoint.broker,
                        "name": f"{endpoint.broker} {endpoint.destination}",
                        "display_name": f"{endpoint.broker} {endpoint.destination}",
                        "destination": endpoint.destination,
                    },
                ),
            )
            if endpoint.declaration_id is None:
                source = file_node(endpoint.file)
            else:
                source = symbol_node(endpoint.declaration_id)
            relationships.append(
                RelationshipData(
                    "PUBLISHES" if endpoint.role == "publish" else "CONSUMES",
                    source[0],
                    {"qualified_name": source[1], "repo_url": repo_url},
                    ["MessageDestination", "Contract"],
                    {"qualified_name": name, "repo_url": repo_url},
                    {
                        "endpoint_id": endpoint.id,
                        "client": endpoint.client,
                        "line": endpoint.line,
                        "column": endpoint.column,
                        **({"service": endpoint.service} if endpoint.service else {}),
                        **(
                            {"service_module": endpoint.module}
                            if endpoint.module
                            else {}
                        ),
                        **(
                            {
                                "service_identity_rule": IDENTITY_RULES[
                                    endpoint.service_identity
                                ].rule_id
                            }
                            if endpoint.service_identity
                            else {}
                        ),
                        **(
                            {"deployed_as": list(endpoint.deployed_as)}
                            if endpoint.deployed_as
                            else {}
                        ),
                        **(
                            {
                                "argument_from": list(endpoint.argument_from),
                                "site_rule": MESSAGE_RULES[
                                    (endpoint.client, endpoint.role)
                                ].rule_id,
                                **_evidence(ARGUMENT_RULE),
                            }
                            if endpoint.argument_from
                            else _evidence(
                                MESSAGE_RULES[(endpoint.client, endpoint.role)]
                            )
                        ),
                    },
                )
            )
        nodes.extend(topics[name] for name in sorted(topics))
        unread_counts = {}
        for item in unread:
            key = f"{item.kind}:{item.reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        handled = {r.declared_in for r in routes if r.declared_in}
        # An operation in several interfaces (several tags) counts once.
        for declared in sorted(
            {
                f"{operation.spec_file}:{operation.spec_line}"
                for operations in generated.values()
                for operation in operations
            }
            - handled
        ):
            # No controller in the repository overrides it.
            key = "openapi:operation_not_handled"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        for reason, count in graphql_unread.items():
            unread_counts[f"graphql:{reason}"] = count
        for item in openapi_unread:
            key = f"openapi:{item.reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        for item in gateway_unread:
            key = f"gateway:{item.reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        for _, _, kind, reason in grpc.unread:
            key = f"grpc_{kind}:{reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        for item in messaging_unread:
            key = f"{item.role}:{item.reason}"
            unread_counts[key] = unread_counts.get(key, 0) + 1
        nodes = (
            sorted(files.values(), key=lambda n: n.properties["qualified_name"])
            + sorted(symbols.values(), key=lambda n: n.properties["qualified_name"])
            + nodes
        )
        artifact = result.to_dict()
        diagnostics = artifact["diagnostics"]
        metadata = ToolExecutionMetadata(
            tool_name=self.metadata.name,
            tool_version=self.metadata.version,
            execution_time_seconds=time.monotonic() - started,
            languages_processed=sorted({c.language for c in result.coverage}),
            files_processed=len(result.coverage),
            errors=(
                [d["message"] for d in diagnostics] if result.status == "failed" else []
            ),
            warnings=(
                [d["message"] for d in diagnostics] if result.status != "failed" else []
            ),
            custom_metrics={
                "analysis_status": result.status,
                "model_tokens": 0,
                "snapshot_id": result.snapshot_id,
                "repository_id": result.repository_id,
                "coverage": artifact["coverage"],
                "diagnostics": diagnostics,
                "http_routes": len(routes),
                "http_requests": len(requests),
                "http_request_resolution": dict(sorted(counts.items())),
                "unread": dict(sorted(unread_counts.items())),
                "gateway_routes": len(gateways),
                "message_endpoints": len(endpoints),
                "message_destinations": len(topics),
                "test_source_files_skipped": test_files,
                "graphql_fields": len(graphql_fields),
                "graphql_requests": len(graphql_requests),
                "grpc_methods": len(grpc.index.methods),
                "grpc_handlers": len(grpc.handlers),
                "grpc_calls": len(grpc.calls),
                "grpc_call_resolution": dict(sorted(grpc_counts.items())),
                "proto_files": dict(sorted(grpc.proto_files.items())),
                "scope": "Spring MVC, Flask, FastAPI, Express and Go net/http "
                "routes; Feign, RestTemplate, WebClient, requests, httpx, Locust, "
                "fetch, axios, request, Go net/http and C# HttpClient requests with "
                "folded URLs, outside test source sets; services named by their "
                "Compose or Skaffold-built Kubernetes deployments, Locust --host "
                "read from them; requests linked to routes by service "
                "name and path template, never by traced values; gRPC "
                "methods from .proto files, their Go, Python, Java, Node and C# "
                "servers and stub calls, matched by proto service and rpc name",
            },
        )
        return StandardizedOutput(
            nodes=nodes, relationships=relationships, metadata=metadata
        )

    async def get_symbol_definitions(self):
        return {}
