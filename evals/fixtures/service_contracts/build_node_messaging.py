"""Build the Node messaging contract-link ground truth JSONs.

Labelled by reading each repository at its pinned commit, separately from the
kafkajs and amqplib readers (#168): nobody looked at their code or tests, and
the repositories are not the ones their validation used. Run
`CORPORA=<clones> python3 build_node_messaging.py <outdir>`.
"""

import sys

from corpus_builder import Corpus

OUT = sys.argv[1]

# ------------------------------------------ 19EB/microservices-base-kafka
mb = Corpus(
    "microservices-base-kafka",
    corpus="node-microservices-base-kafka",
    repo="https://github.com/19EB/microservices-base-kafka",
    license="MIT",
    languages=["TypeScript (Node, Express, kafkajs)"],
    sources=[
        "README.md (each service listens on its own topic; the others ping it through Kafka)",
        "manual reading of services/*/src and api-gateway at the pinned commit",
    ],
    services=[
        {"name": "api-gateway", "root": "api-gateway"},
        {"name": "user", "root": "services/user"},
        {"name": "client", "root": "services/client"},
        {"name": "product", "root": "services/product"},
    ],
    notes=[
        "Each service has its own copy of src/util/broker.ts: KafkaPublish(kafka, topic, payload) "
        "calls producer.send({topic: topic}) on kafka.producer(), and KafkaSubscribe subscribes "
        "kafka.consumer() to the file's constant KAFKA_TOPIC ('<service>-topic'). The Kafka "
        "client is made by CreateKafkaClient (new Kafka(...)) and passed in as a parameter.",
        "client is the producer.send({ line in the publishing service's broker.ts, which every "
        "topic that service publishes shares; producer_invocation is the KafkaPublish(kafka, "
        "'<topic>', ...) call in src/api that fixes the topic. server is the consumer.subscribe "
        "line in the consuming service's broker.ts.",
        "api-gateway proxies /user, /client and /product to the services with "
        "express-http-proxy (URLs from .env.dev): prefix routes, not labelled here.",
    ],
)
SERVICES = ("user", "client", "product")
for frm in SERVICES:
    api = f"services/{frm}/src/api/{frm}-api.ts"
    send = mb.loc(f"services/{frm}/src/util/broker.ts", "await producer.send({")
    for to in SERVICES:
        if to == frm:
            continue
        mb.link(
            "messaging", frm, to, f"kafka-topic:{to}-topic", send,
            mb.loc(f"services/{to}/src/util/broker.ts", "await consumer.subscribe({ topic: KAFKA_TOPIC"),
            f"KafkaPublish(kafka, '{to}-topic') -> producer.send({{topic}}); "
            f"{to} broker.ts subscribe({{topic: KAFKA_TOPIC = '{to}-topic'}})",
            producer_invocation=mb.loc(api, f"KafkaPublish(kafka, '{to}-topic'"),
            topic_config=[mb.loc(f"services/{to}/src/util/broker.ts", f"const KAFKA_TOPIC = '{to}-topic';")],
        )
mb.write(OUT)
