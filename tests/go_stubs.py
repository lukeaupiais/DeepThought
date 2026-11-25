"""Stand-ins for Go broker client modules, for tests that run go/types.

Each is the part of a client library's API the tests call, in a module
directory of its own; ``go_mod`` writes a go.mod that requires the modules
and replaces them with these directories, so ``go mod download`` and the
type checker read them without the network.
"""

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

type ProducerMessage struct{ Topic string }
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
type Message struct{ TopicPartition TopicPartition }
type Producer struct{}

func (p *Producer) Produce(msg *Message, deliveryChan chan Event) error { return nil }

type RebalanceCb func(*Consumer, Event) error
type Consumer struct{}

func (c *Consumer) SubscribeTopics(topics []string, cb RebalanceCb) error { return nil }
func (c *Consumer) Subscribe(topic string, cb RebalanceCb) error { return nil }
""",
    },
}
STUBS["github.com/rabbitmq/amqp091-go"] = {
    "channel.go": STUBS["github.com/streadway/amqp"]["channel.go"].replace(
        "package amqp\n", "package amqp091\n", 1
    )
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
