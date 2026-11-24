"""GraphQL subscription pubsub: PubSub and RedisPubSub topics that
resolvers publish to and subscribe to."""

import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from deepthought.analysis_tools import ServiceContractTool
from deepthought.analysis_tools.evidence import get_rule

READY = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)

RESOLVERS = """
import { PubSub } from 'graphql-subscriptions';

const POST_ADDED = 'POST_ADDED';
const pubsub = new PubSub();

export const resolvers = {
    Mutation: {
        addPost(_, post) {
            pubsub.publish(POST_ADDED, { postAdded: post });
            pubsub.publish(post.kind, post);
            return post;
        },
    },
    Subscription: {
        postAdded: { subscribe: () => pubsub.asyncIterator(POST_ADDED) },
        anything: { subscribe: () => pubsub.asyncIterator(['A', 'B']) },
    },
};
"""

# Bound to a class field, from the PubSub that @nestjs/graphql re-exports.
NEST = """
import { PubSub } from '@nestjs/graphql';

export class CommentsResolver {
    private pubSub = new PubSub();

    addComment(comment) {
        this.pubSub.publish('commentAdded', { commentAdded: comment });
    }

    commentAdded() {
        return this.pubSub.asyncIterableIterator('commentAdded');
    }
}
"""

# Another in-memory PubSub: its POST_ADDED is not the one above.
OTHER = """
import { PubSub } from 'graphql-subscriptions';

const pubsub = new PubSub();
pubsub.asyncIterator('POST_ADDED');

// Rebound to something else: not a PubSub.
let local = new PubSub();
local = {};
local.publish('ignored');
"""

REDIS_PUBLISHER = """
import { RedisPubSub } from 'graphql-redis-subscriptions';

const pubsub = new RedisPubSub();
pubsub.publish('news', { news: 'hi' });
"""

REDIS_SUBSCRIBER = """
import { RedisPubSub } from 'graphql-redis-subscriptions';

const events = new RedisPubSub({ connection: {} });
export const subscribe = () => events.asyncIterator('news');
"""

FILES = {
    "api/package.json": '{"name": "api"}',
    "api/src/resolvers.ts": RESOLVERS,
    "api/src/comments.resolver.ts": NEST,
    "api/src/other.ts": OTHER,
    "api/src/publisher.ts": REDIS_PUBLISHER,
    "worker/package.json": '{"name": "worker"}',
    "worker/src/subscriber.ts": REDIS_SUBSCRIBER,
}


@unittest.skipUnless(READY, "tree-sitter grammars are not installed")
class GraphqlPubSubTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for name, text in FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.output = asyncio.run(ServiceContractTool(root).analyze("r"))

    def tearDown(self):
        self.directory.cleanup()

    def test_edges(self):
        edges = sorted(
            (
                r.target_match["qualified_name"].split("::", 3)[-1],
                r.type,
                r.properties["client"],
                r.source_match["qualified_name"].split("::")[1].rpartition("/")[2],
                r.properties["line"],
            )
            for r in self.output.relationships
            if r.type in ("PUBLISHES", "CONSUMES")
        )
        self.maxDiff = None
        nest = "api/src/comments.resolver.ts#this.pubSub/commentAdded"
        resolvers = "api/src/resolvers.ts#pubsub/"
        client, redis = "graphql-subscriptions", "graphql-redis-subscriptions"
        self.assertEqual(
            edges,
            [
                (nest, "CONSUMES", client, "comments.resolver.ts", 12),
                (nest, "PUBLISHES", client, "comments.resolver.ts", 8),
                (
                    "api/src/other.ts#pubsub/POST_ADDED",
                    "CONSUMES",
                    client,
                    "other.ts",
                    5,
                ),
                # An array of topics names each one.
                (resolvers + "A", "CONSUMES", client, "resolvers.ts", 17),
                (resolvers + "B", "CONSUMES", client, "resolvers.ts", 17),
                (resolvers + "POST_ADDED", "CONSUMES", client, "resolvers.ts", 16),
                (resolvers + "POST_ADDED", "PUBLISHES", client, "resolvers.ts", 10),
                # Redis channels meet across services.
                ("news", "CONSUMES", redis, "subscriber.ts", 5),
                ("news", "PUBLISHES", redis, "publisher.ts", 5),
            ],
        )

    def test_unread(self):
        unread = self.output.metadata.custom_metrics["unread"]
        # publish(post.kind, ...); the rebound name is not read at all.
        self.assertEqual(unread, {"publish:destination_not_literal": 1})

    def test_rules(self):
        for rule_id in (
            "service-contracts.graphql-pubsub-publish",
            "service-contracts.graphql-pubsub-subscribe",
            "service-contracts.graphql-redis-pubsub-publish",
            "service-contracts.graphql-redis-pubsub-subscribe",
        ):
            self.assertEqual(get_rule(rule_id).evidence_type, "pattern_matched")


if __name__ == "__main__":
    unittest.main()
