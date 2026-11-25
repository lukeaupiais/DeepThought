"""Database tables: SQL DDL, JPA entity mappings and Spring Data repositories."""

import asyncio
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from deepthought.analysis_tools import (
    ComponentsTool,
    DataContractTool,
    TreeSitterTool,
)
from deepthought.analysis_tools.evidence import get_rule
from deepthought.go_facts import GoFacts, GoMissing
from deepthought.data_contracts import (
    UNKNOWN,
    default_table_name,
    extract_ddl,
    extract_schema_changes,
    extract_foreign_keys,
    migration_set,
    mongoose_majors,
    mongoose_pluralize,
    statement_tables,
)
from deepthought.service_contracts.spring import _text_block
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinatorConfig,
    _combine,
)
from deploy_tools_ready import needs_deploy_tools
from go_stubs import needs_go

JAVA = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_java"
)
PYTHON = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_python"
)
DJANGO = {
    "pyproject.toml": '[project]\nname = "site"\n',
    "shop/apps.py": "from django.apps import AppConfig\n"
    "class ShopConfig(AppConfig):\n"
    '    name = "shop"\n'
    '    label = "store"\n',
    "shop/models/__init__.py": "from .orders import Order\n",
    "shop/models/base.py": "from django.db import models\n"
    "class Tagged(models.Model):\n"
    "    tags = models.ManyToManyField('Tag')\n"
    "    class Meta:\n"
    "        abstract = True\n",
    "shop/models/orders.py": "from django.db import models\n"
    "from django.db.models import Model as DjangoModel, ManyToManyField\n"
    "from .base import Tagged\n"
    "class Tag(DjangoModel):\n"
    "    class Meta:\n"
    '        db_table = "Shop_Tags"\n'
    "class Order(Tagged):\n"
    "    items = ManyToManyField(Tag, through='Line')\n"
    "class Line(models.Model):\n"
    "    class Meta:\n"
    '        app_label = "sales"\n'
    "class Urgent(Order):\n"
    "    class Meta:\n"
    "        proxy = True\n"
    "class Plain:\n"
    "    pass\n",
    "shop/views.py": "from .models import Order, Tag\n"
    "TOTAL = Order.objects.count()\n"
    "def close(pk):\n"
    "    Order.objects.filter(pk=pk).update(open=False)\n"
    "    return Tag.objects.get_or_create(name='closed')\n"
    "def urgent():\n"
    "    return list(Urgent.objects.all())\n"
    "def other():\n"
    "    return Plain.objects.all()\n",
    "shop/migrations/0002_close.py": "def close(apps, schema_editor):\n"
    '    Order = apps.get_model("store", "Order")\n'
    "    Order.objects.filter(open=True).update(open=False)\n",
}

MODEL = "store/src/main/java/shop/model/"
WORKSPACE = {
    "store/pom.xml": "<project><artifactId>store</artifactId></project>",
    "store/src/main/resources/schema.sql": (
        "-- CREATE TABLE commented_out (id INT);\n"
        "CREATE TABLE IF NOT EXISTS public.Orders (id INT);\n"
        "create table `line_item` (id int);\n"
        "CREATE TEMPORARY TABLE scratch (id INT);\n"
        "INSERT INTO orders VALUES ('CREATE TABLE in_a_string (x INT)');\n"
    ),
    MODEL + "Order.java": "package shop.model;\n"
    "import jakarta.persistence.*;\n"
    "@Entity\n"
    "@Table(name = Order.TABLE)\n"
    "public class Order {\n"
    '  static final String TABLE = "orders";\n'
    "  @ManyToMany\n"
    '  @JoinTable(name = "order_tags")\n'
    "  java.util.Set<String> tags;\n"
    "}\n",
    MODEL + "LineItem.java": "package shop.model;\n"
    "import javax.persistence.Entity;\n"
    "@Entity\n"
    "public class LineItem { }\n",
    MODEL + "Audit.java": "package shop.model;\n"
    "import org.hibernate.annotations.Entity;\n"
    "@Entity\n"
    "public class Audit { }\n",
    "store/src/main/java/shop/data/OrderRepository.java": "package shop.data;\n"
    "import org.springframework.data.jpa.repository.JpaRepository;\n"
    "import shop.model.Order;\n"
    "public interface OrderRepository extends JpaRepository<Order, Long> { }\n",
    "store/src/main/java/shop/data/LineItemReader.java": "package shop.data;\n"
    "import org.springframework.data.repository.PagingAndSortingRepository;\n"
    "import shop.model.*;\n"
    "public interface LineItemReader\n"
    "    extends PagingAndSortingRepository<shop.model.LineItem, Long> { }\n",
    "store/src/main/java/shop/data/Other.java": "package shop.data;\n"
    "import com.example.JpaRepository;\n"
    "import shop.model.Order;\n"
    "public interface Other extends JpaRepository<Order, Long> { }\n",
    "store/src/main/java/shop/data/Unknown.java": "package shop.data;\n"
    "import org.springframework.data.repository.CrudRepository;\n"
    "public interface Unknown extends CrudRepository<Missing, Long> { }\n",
    "store/src/test/java/shop/TestRepo.java": "package shop;\n"
    "import org.springframework.data.repository.CrudRepository;\n"
    "import shop.model.Order;\n"
    "public interface TestRepo extends CrudRepository<Order, Long> { }\n",
    "report/pom.xml": "<project><artifactId>report</artifactId></project>",
    "report/src/main/resources/db/V1__init.sql": "CREATE TABLE orders (id INT);\n",
}


def shared_datasource_files():
    """Three services; billing and report share one persistent database."""
    entity = (
        "package {p};\nimport jakarta.persistence.*;\n"
        '@Entity @Table(name = "orders") public class Order {{ }}\n'
    )
    repository = (
        "package {p};\nimport org.springframework.data.repository.{base};\n"
        "public interface Orders extends {base}<Order, Long> {{ }}\n"
    )
    files = {}
    for module, url, base in (
        ("billing", "jdbc:postgresql://db:5432/shop", "CrudRepository"),
        ("report", "jdbc:postgresql://db:5432/shop", "PagingAndSortingRepository"),
        ("cache", "jdbc:h2:mem:cache", "PagingAndSortingRepository"),
    ):
        java = f"{module}/src/main/java/{module}/"
        files[f"{module}/pom.xml"] = (
            f"<project><artifactId>{module}</artifactId></project>"
        )
        files[f"{module}/src/main/resources/application.yml"] = (
            f"spring:\n  datasource:\n    url: {url}\n"
        )
        files[java + "Order.java"] = entity.format(p=module)
        files[java + "Orders.java"] = repository.format(p=module, base=base)
    return files


def write(directory, files):
    for name, text in files.items():
        path = Path(directory) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class DdlTests(unittest.TestCase):
    def test_create_table_statements(self):
        found = extract_ddl(
            "s.sql", WORKSPACE["store/src/main/resources/schema.sql"].encode()
        )
        self.assertEqual(
            [(d.line, d.name, d.schema) for d in found],
            [(2, "orders", "public"), (3, "line_item", None)],
        )
        self.assertEqual(
            [
                d.name
                for d in extract_ddl("q.sql", b'CREATE TABLE "Mixed Case" (x INT);')
            ],
            ["mixed case"],
        )
        self.assertIsNone(extract_ddl("bad.sql", b"\xff\xfe"))

    def test_create_table_readers(self):
        text = b"""-- a comment; not a statement end
CREATE TEMPORARY TABLE scratch (x INT);
CREATE FUNCTION f() RETURNS void AS $$ BEGIN CREATE TABLE inner_t (x INT); END $$ LANGUAGE plpgsql;
CREATE TABLE IF NOT EXISTS events
(
  id String CODEC(ZSTD(1))
) ENGINE = MergeTree ORDER BY id;
CREATE TABLE "Ids" (
  seq bigint GENERATED ALWAYS AS IDENTITY (sequence name "s" INCREMENT BY 1),
  n int
);
"""
        found = extract_ddl("m.sql", text)
        self.assertEqual(
            [(d.line, d.name, d.schema, d.reader) for d in found],
            [
                (4, "events", None, "sqlglot"),
                (8, "ids", None, "tokens_not_parsed"),
            ],
        )

    def test_foreign_keys(self):
        text = b"""CREATE TABLE users (id int PRIMARY KEY);
CREATE TABLE orders (
  id int,
  user_id int REFERENCES public.users (id) ON DELETE CASCADE,
  a int, b int,
  CONSTRAINT pair FOREIGN KEY (a, b) REFERENCES "Pairs" (x, y)
);
CREATE TEMPORARY TABLE scratch (u int REFERENCES users (id));
ALTER TABLE ONLY public.orders
  ADD CONSTRAINT owner FOREIGN KEY (user_id) REFERENCES public.users(id);
CREATE TABLE `m` (`x` int, FOREIGN KEY (`x`) REFERENCES `users` (`id`));
CREATE TABLE odd (x int REFERENCES users NOT SQL AT ALL (((;
"""
        found, unparsed = extract_foreign_keys("m.sql", text)
        self.assertEqual(
            sorted(
                (
                    k.line,
                    k.table,
                    k.references,
                    k.columns,
                    k.referenced_columns,
                    k.on_delete,
                )
                for k in found
            ),
            [
                (4, "orders", "users", ("user_id",), ("id",), "CASCADE"),
                (6, "orders", "pairs", ("a", "b"), ("x", "y"), None),
                (10, "orders", "users", ("user_id",), ("id",), None),
                (11, "m", "users", ("x",), ("id",), None),
            ],
        )
        self.assertEqual(unparsed, [12])
        self.assertIsNone(extract_foreign_keys("bad.sql", b"\xff\xfe"))

    def test_foreign_key_edges(self):
        files = {
            "pyproject.toml": '[project]\nname = "shop"\n',
            "db/schema.sql": "CREATE TABLE users (id int);\n"
            "CREATE TABLE orders (id int, user_id int REFERENCES users (id));\n"
            "CREATE TABLE odd (x int REFERENCES users NOT SQL (((;\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
            )
        keys = [r for r in output.relationships if r.type == "REFERENCES_TABLE"]
        self.assertEqual(
            [
                (
                    r.source_match["qualified_name"].rsplit("::", 1)[-1],
                    r.target_match["qualified_name"].rsplit("::", 1)[-1],
                    r.properties["rule_id"],
                    r.properties["columns"],
                    r.properties["line"],
                )
                for r in keys
            ],
            [
                (
                    "orders",
                    "users",
                    "data-contracts.sql-foreign-key",
                    ["user_id"],
                    2,
                )
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["foreign_keys"],
            {
                "sql": 1,
                "prisma": 0,
                "sql_not_parsed": 1,
                "sql_dropped": 0,
                "sql_drops_naming_no_key": 0,
                "sql_rollback_files": 0,
                "drizzle_snapshot": 0,
                "drizzle_snapshot_unread": 0,
                "prisma_migration_files_superseded": 0,
            },
        )
        self.assertIn(
            "foreign_key_not_parsed: db/schema.sql:3: no sqlglot dialect parses "
            "this statement, so its foreign keys are not read",
            output.metadata.warnings,
        )

    def test_drizzle_snapshot_states_the_foreign_keys(self):
        snapshot = {
            "version": "7",
            "dialect": "postgresql",
            "tables": {
                "public.posts": {
                    "name": "posts",
                    "foreignKeys": {
                        "posts_author_id_users_id_fk": {
                            "name": "posts_author_id_users_id_fk",
                            "tableFrom": "posts",
                            "tableTo": "users",
                            "columnsFrom": ["author_id"],
                            "columnsTo": ["id"],
                            "onDelete": "cascade",
                        }
                    },
                },
                "public.users": {"name": "users", "foreignKeys": {}},
            },
        }
        files = {
            "package.json": '{"name": "app"}',
            "drizzle/meta/_journal.json": '{"entries": [{"idx": 0}, {"idx": 1}]}',
            # The first snapshot still has the key the second migration drops.
            "drizzle/meta/0000_snapshot.json": "{}",
            "drizzle/meta/0001_snapshot.json": json.dumps(snapshot, indent=2),
            "drizzle/0000_init.sql": "CREATE TABLE users (id int);\n"
            "CREATE TABLE posts (id int, author_id int);\n"
            "CREATE TABLE tags (id int, user_id int REFERENCES users (id));\n"
            "DO $$ BEGIN ALTER TABLE posts ADD CONSTRAINT posts_author_id_users_id_fk"
            " FOREIGN KEY (author_id) REFERENCES users (id); END $$;\n",
            "drizzle/0001_drop.sql": "DROP TABLE tags;\n",
            "broken/meta/_journal.json": '{"entries": []}',
            "broken/0000_init.sql": "CREATE TABLE a (b int REFERENCES c (id));\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
            )
        keys = [r for r in output.relationships if r.type == "REFERENCES_TABLE"]
        self.assertEqual(
            [
                (
                    r.source_match["qualified_name"].rsplit("::", 1)[-1],
                    r.target_match["qualified_name"].rsplit("::", 1)[-1],
                    r.properties["rule_id"],
                    r.properties["file_path"],
                    r.properties["line"],
                    r.properties.get("on_delete"),
                )
                for r in keys
            ],
            [
                (
                    "a",
                    "c",
                    "data-contracts.sql-foreign-key",
                    "broken/0000_init.sql",
                    1,
                    None,
                ),
                (
                    "posts",
                    "users",
                    "data-contracts.drizzle-foreign-key",
                    "drizzle/meta/0001_snapshot.json",
                    8,
                    "CASCADE",
                ),
            ],
        )
        metrics = output.metadata.custom_metrics["foreign_keys"]
        # The folder whose last snapshot is missing is read migration by
        # migration, and the warning says so.
        self.assertEqual(
            (metrics["sql"], metrics["drizzle_snapshot"]),
            (1, 1),
        )
        self.assertEqual(metrics["drizzle_snapshot_unread"], 1)
        self.assertTrue(
            any(
                w.startswith("drizzle_snapshot_unread: drizzle-kit snapshot of broken")
                for w in output.metadata.warnings
            ),
            output.metadata.warnings,
        )

    def test_foreign_key_drops(self):
        text = b"""ALTER TABLE "Signature" DROP CONSTRAINT "Signature_fieldId_fkey";
ALTER TABLE IF EXISTS accounts DROP CONSTRAINT IF EXISTS accounts_owner_fkey;
ALTER TABLE m DROP FOREIGN KEY m_ibfk_1;
DROP TABLE IF EXISTS "Share", tags CASCADE;
ALTER TABLE a DROP COLUMN b;
DROP INDEX i;
ALTER TABLE "posts_new" RENAME TO "Posts";
ALTER TABLE a RENAME COLUMN x TO y;
RENAME TABLE m TO n;
"""
        found, unparsed = extract_schema_changes("m.sql", text)
        self.assertEqual(
            [(d.line, d.table, getattr(d, "constraint", d)) for d in found[:5]],
            [
                (1, "signature", "signature_fieldid_fkey"),
                (2, "accounts", "accounts_owner_fkey"),
                (3, "m", "m_ibfk_1"),
                (4, "share", None),
                (4, "tags", None),
            ],
        )
        self.assertEqual(
            [(r.line, r.table, r.new_name) for r in found[5:]],
            [(7, "posts_new", "posts")],
        )
        # MySQL's RENAME TABLE has no sqlglot parse: its line is reported,
        # not read as no change.
        self.assertEqual(unparsed, [9])

    def test_migration_sets(self):
        self.assertEqual(
            [
                migration_set(p)
                for p in (
                    "prisma/migrations/20230404_initial_migration/migration.sql",
                    "db/migration/V2__users.sql",
                    "schema.sql",
                )
            ],
            ["prisma/migrations", "db/migration", "schema.sql"],
        )

    def test_migrations_replay_dropped_foreign_keys(self):
        files = {
            "package.json": '{"name": "app"}',
            "db/migrations/V1__init.sql": "CREATE TABLE users (id int);\n"
            "CREATE TABLE posts (id int, author_id int REFERENCES users (id));\n"
            "CREATE TABLE tags (id int, user_id int REFERENCES users (id));\n"
            "CREATE TABLE notes (id int, post_id int,\n"
            "  CONSTRAINT notes_post FOREIGN KEY (post_id) REFERENCES posts (id));\n",
            # V2 sorts before V10: the key is changed, then the table dropped.
            "db/migrations/V2__on_delete.sql": "ALTER TABLE notes DROP CONSTRAINT "
            "notes_post;\nALTER TABLE notes ADD CONSTRAINT notes_post FOREIGN KEY "
            "(post_id) REFERENCES posts (id) ON DELETE CASCADE;\n"
            "ALTER TABLE posts DROP CONSTRAINT posts_pkey;\n",
            "db/migrations/V10__drop.sql": "DROP TABLE tags;\n"
            "ALTER TABLE posts DROP CONSTRAINT posts_author_id_fkey;\n",
            # A rollback is not part of the schema the migrations build.
            "db/migrations/V10__drop.down.sql": "CREATE TABLE tags (id int, "
            "user_id int REFERENCES users (id));\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
            )
        keys = [r for r in output.relationships if r.type == "REFERENCES_TABLE"]
        self.assertEqual(
            [
                (
                    r.source_match["qualified_name"].rsplit("::", 1)[-1],
                    r.target_match["qualified_name"].rsplit("::", 1)[-1],
                    r.properties["file_path"],
                    r.properties["line"],
                    r.properties.get("on_delete"),
                )
                for r in keys
            ],
            [("notes", "posts", "db/migrations/V2__on_delete.sql", 2, "CASCADE")],
        )
        metrics = output.metadata.custom_metrics["foreign_keys"]
        self.assertEqual(
            (
                metrics["sql"],
                metrics["sql_dropped"],
                metrics["sql_drops_naming_no_key"],
                metrics["sql_rollback_files"],
            ),
            (1, 3, 1, 1),
        )

    def test_migrations_replay_dropped_and_renamed_tables(self):
        files = {
            "package.json": '{"name": "app"}',
            "db/migrations/0001_init.sql": "CREATE TABLE users (id int);\n"
            "CREATE TABLE tags (id int);\n"
            "CREATE TABLE posts (id int, author_id int REFERENCES users (id));\n",
            "db/migrations/0002_swap.sql": "CREATE TABLE posts_new (id int,\n"
            "  author_id int REFERENCES users (id) ON DELETE CASCADE);\n"
            "DROP TABLE posts;\n"
            'ALTER TABLE "posts_new" RENAME TO "posts";\n'
            "DROP TABLE tags;\n",
            "db/migrations/0002_swap.down.sql": "CREATE TABLE tags (id int);\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
            )
        declared = sorted(
            (
                r.target_match["qualified_name"].rsplit("::", 1)[-1],
                r.source_match["qualified_name"].split("::")[-2],
                r.properties["line"],
            )
            for r in output.relationships
            if r.type == "DECLARES"
        )
        self.assertEqual(
            declared,
            [
                ("posts", "db/migrations/0002_swap.sql", 4),
                ("users", "db/migrations/0001_init.sql", 1),
            ],
        )
        keys = [
            (
                r.source_match["qualified_name"].rsplit("::", 1)[-1],
                r.target_match["qualified_name"].rsplit("::", 1)[-1],
                r.properties.get("on_delete"),
            )
            for r in output.relationships
            if r.type == "REFERENCES_TABLE"
        ]
        self.assertEqual(keys, [("posts", "users", "CASCADE")])
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            (
                metrics["ddl_statements"],
                metrics["ddl_tables_dropped"],
                metrics["ddl_tables_renamed"],
            ),
            (5, 2, 1),
        )

    def test_default_physical_names(self):
        self.assertEqual(
            [
                default_table_name(n)
                for n in ("LineItem", "Order", "HTTPRequestLog", "Pet2Owner")
            ],
            ["line_item", "order", "httprequest_log", "pet2_owner"],
        )


@unittest.skipUnless(JAVA, "Provision the java grammar")
class ToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        write(self.directory.name, WORKSPACE)

    def analyze(self, repo="r"):
        return asyncio.run(DataContractTool(self.directory.name).analyze(repo))

    def test_tables_mappings_and_accesses(self):
        output = self.analyze()
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2]
                if r.type != "DECLARES"
                else r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].split("::", 1)[1],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        store = "store/pom.xml::db-table::"
        self.assertEqual(
            edges,
            [
                (
                    "DECLARES",
                    "report/src/main/resources/db/V1__init.sql",
                    "report/pom.xml::db-table::orders",
                    "data-contracts.sql-create-table",
                ),
                (
                    "DECLARES",
                    "store/src/main/resources/schema.sql",
                    store + "line_item",
                    "data-contracts.sql-create-table",
                ),
                (
                    "DECLARES",
                    "store/src/main/resources/schema.sql",
                    store + "orders",
                    "data-contracts.sql-create-table",
                ),
                (
                    "MAPS_TO",
                    "LineItem",
                    store + "line_item",
                    "data-contracts.jpa-default-table-name",
                ),
                (
                    "MAPS_TO",
                    "Order",
                    store + "order_tags",
                    "data-contracts.jpa-join-table",
                ),
                (
                    "MAPS_TO",
                    "Order",
                    store + "orders",
                    "data-contracts.jpa-table-annotation",
                ),
                (
                    "READS",
                    "LineItemReader",
                    store + "line_item",
                    "data-contracts.spring-data-repository",
                ),
                (
                    "READS",
                    "OrderRepository",
                    store + "orders",
                    "data-contracts.spring-data-repository",
                ),
                (
                    "WRITES",
                    "OrderRepository",
                    store + "orders",
                    "data-contracts.spring-data-repository",
                ),
            ],
        )
        for r in output.relationships:
            rule = get_rule(r.properties["rule_id"])
            self.assertEqual(r.properties["evidence_type"], rule.evidence_type)
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            metrics["repository_resolution"], {"linked": 2, "not_an_entity": 1}
        )
        self.assertEqual(metrics["tables"], 4)
        self.assertEqual(metrics["association_tables"], 1)
        tables = [n for n in output.nodes if "DbTable" in n.labels]
        self.assertTrue(all("Contract" in n.labels for n in tables))
        # Same facts, same output.
        again = self.analyze()
        self.assertEqual(
            [n.properties for n in output.nodes], [n.properties for n in again.nodes]
        )
        self.assertEqual(
            metrics["snapshot_id"], again.metadata.custom_metrics["snapshot_id"]
        )

    def test_one_persistent_datasource_shares_its_tables(self):
        files = shared_datasource_files()
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            outputs = [
                asyncio.run(TreeSitterTool(directory).analyze("r", languages={"java"})),
                asyncio.run(ComponentsTool(directory).analyze("r")),
                asyncio.run(DataContractTool(directory).analyze("r")),
            ]
        tables = sorted(
            n.properties["qualified_name"]
            for n in outputs[2].nodes
            if "DbTable" in n.labels
        )
        self.assertEqual(
            tables,
            [
                "r::cache/pom.xml::db-table::orders",
                "r::datasource:jdbc:postgresql://db:5432/shop::db-table::orders",
            ],
        )
        merged, _ = _combine(outputs, "r", "rev", WriteCoordinatorConfig())
        self.assertEqual(
            [
                (
                    r.source_match["qualified_name"],
                    r.target_match["qualified_name"],
                    r.properties["rule_id"],
                )
                for r in merged.relationships
                if r.type == "USES_CONTRACT_OF"
            ],
            [
                (
                    "r::report/pom.xml::maven_module::component",
                    "r::billing/pom.xml::maven_module::component",
                    "components.contract-consumer",
                )
            ],
        )

    def test_combined_with_tree_sitter_and_components(self):
        repo = "r"
        outputs = [
            asyncio.run(
                TreeSitterTool(self.directory.name).analyze(repo, languages={"java"})
            ),
            asyncio.run(ComponentsTool(self.directory.name).analyze(repo)),
            self.analyze(repo),
        ]
        merged, _ = _combine(outputs, repo, "rev", WriteCoordinatorConfig())
        symbols = {
            n.properties["qualified_name"]
            for n in outputs[0].nodes
            if "Symbol" in n.labels
        }
        sources = {
            r.source_match["qualified_name"]
            for r in outputs[2].relationships
            if r.type in ("MAPS_TO", "READS", "WRITES")
        }
        self.assertTrue(sources <= symbols)
        declared = [
            r
            for r in merged.relationships
            if r.type == "DECLARES" and "DbTable" in r.target_labels
        ]
        self.assertEqual(len(declared), 3)


@unittest.skipUnless(PYTHON, "Provision the python grammar")
class DjangoTests(unittest.TestCase):
    TABLES = [
        ("MAPS_TO", "Line", "sales_line", "django-default-table-name", None),
        ("MAPS_TO", "Order", "store_order", "django-default-table-name", None),
        ("MAPS_TO", "Order", "store_order_tags", "django-many-to-many-table", "tags"),
        ("MAPS_TO", "Tag", "shop_tags", "django-db-table", None),
    ]

    def analyze(self, with_index):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DJANGO)
            indexes = (self.index(directory),) if with_index else ()
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["python"], scip_indexes=indexes
                )
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
                r.properties.get("member"),
            )
            for r in output.relationships
        )
        return edges, output.metadata

    def index(self, directory):
        """A scip-python index of DJANGO's model names, ingested. Urgent is
        not imported in views.py, so the index records no reference there."""
        import deepthought
        from deepthought._scip import scip_pb2 as wire

        orders = "scip-python python site 0 `shop.models.orders`/"
        documents = {
            "shop/models/orders.py": [
                (3, "Tag", orders + "Tag#", True),
                (6, "Order", orders + "Order#", True),
                (8, "Line", orders + "Line#", True),
                (11, "Urgent", orders + "Urgent#", True),
                (11, "Order", orders + "Order#", False),
                (14, "Plain", orders + "Plain#", True),
            ],
            "shop/migrations/0002_close.py": [
                (1, "Order", "local 0", True),
                (2, "Order", "local 0", False),
            ],
            "shop/views.py": [
                (0, "Order", orders + "Order#", False),
                (0, "Tag", orders + "Tag#", False),
                (1, "Order", orders + "Order#", False),
                (3, "Order", orders + "Order#", False),
                (4, "Tag", orders + "Tag#", False),
                (8, "Plain", orders + "Plain#", False),
            ],
        }
        index = wire.Index()
        index.metadata.tool_info.name = "scip-python"
        index.metadata.tool_info.version = "0.6.8"
        for path, occurrences in documents.items():
            lines = DJANGO[path].split("\n")
            document = index.documents.add(relative_path=path)
            for line, text, symbol, definition in occurrences:
                column = lines[line].index(text)
                document.occurrences.add(
                    symbol=symbol,
                    range=[line, column, column + len(text)],
                    symbol_roles=wire.Definition if definition else 0,
                )
        path = Path(directory) / ".index.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"

    def test_models_and_tables_without_an_index(self):
        edges, metadata = self.analyze(False)
        self.assertEqual(edges, self.TABLES)
        metrics = metadata.custom_metrics
        self.assertEqual(metrics["django_models"], 4)
        # The indexer decides what a name is: no manager call is read, and
        # the run says so.
        self.assertEqual(
            metrics["django_access_resolution"],
            {
                "linked_scip": 0,
                "historical": 0,
                "not_a_model": 0,
                "scip_no_reference": 0,
                "no_scip_index": 6,
            },
        )
        self.assertEqual(metrics["analysis_status"], "partial")
        self.assertIn(
            "no_scip_index: 2 files calling Django managers have no scip-python "
            "index, so their queries are not read; run deepthought index-scip "
            "and pass the index",
            metadata.warnings,
        )

    def test_manager_calls_resolved_through_a_scip_index(self):
        edges, metadata = self.analyze(True)
        rule = "django-scip-manager-call"
        # The migration's Order is the store app's model, by get_model.
        historical = "django-historical-manager-call"
        self.assertEqual(
            edges,
            sorted(
                self.TABLES
                + [
                    ("READS", "close", "shop_tags", rule, None),
                    ("READS", "close", "store_order", rule, None),
                    ("READS", "close", "store_order", historical, None),
                    ("READS", "shop/views.py", "store_order", rule, None),
                    ("WRITES", "close", "shop_tags", rule, None),
                    ("WRITES", "close", "store_order", rule, None),
                    ("WRITES", "close", "store_order", historical, None),
                ]
            ),
        )
        self.assertEqual(
            metadata.custom_metrics["django_access_resolution"],
            {
                "linked_scip": 3,
                "historical": 1,
                "not_a_model": 1,
                "scip_no_reference": 1,
                "no_scip_index": 0,
            },
        )


SQLALCHEMY = {
    "pyproject.toml": '[project]\nname = "app"\n',
    "app/models.py": "import sqlalchemy as sa\n"
    "from sqlalchemy.orm import DeclarativeBase\n"
    "from mylib import Table\n"
    "class Base(DeclarativeBase):\n"
    "    pass\n"
    "class User(Base):\n"
    '    __tablename__ = "users"\n'
    "class Account(Base):\n"
    '    __tablename__: str = "Accounts"\n'
    'user_tags = sa.Table("user_tags", Base.metadata)\n'
    'report = Table("not_sqlalchemy")\n',
    "app/service.py": "from sqlalchemy import select, delete\n"
    "from .models import User\n"
    "def find(db):\n"
    "    return db.session.query(User).all()\n"
    "def ids():\n"
    "    return select(User.id)\n"
    "def purge():\n"
    "    return delete(User)\n"
    "def other(db):\n"
    "    return db.session.query(Unknown)\n",
    "app/plain.py": 'class Plain:\n    __tablename__ = "not_mapped"\n',
}


@unittest.skipUnless(PYTHON, "Provision the python grammar")
class SqlAlchemyTests(unittest.TestCase):
    def test_mapped_classes_tables_and_statements(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, SQLALCHEMY)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
        )
        self.assertEqual(
            edges,
            [
                ("DECLARES", "app/models.py", "user_tags", "sqlalchemy-table"),
                ("MAPS_TO", "Account", "accounts", "sqlalchemy-tablename"),
                ("MAPS_TO", "User", "users", "sqlalchemy-tablename"),
                ("READS", "find", "users", "sqlalchemy-session-query"),
                ("READS", "ids", "users", "sqlalchemy-statement"),
                ("WRITES", "purge", "users", "sqlalchemy-statement"),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["sqlalchemy_access_resolution"],
            {
                "linked": 3,
                "linked_scip": 0,
                "not_a_mapped_class": 1,
                "scip_not_a_mapped_class": 0,
                "instance_no_scip_index": 0,
                "instance_method_unresolved": 0,
                "instance_not_session": 0,
                "instance_type_unknown": 0,
            },
        )


SQLMODEL = {
    "pyproject.toml": '[project]\nname = "app"\n',
    "app/models.py": "from sqlmodel import SQLModel\n"
    "class ItemBase(SQLModel):\n"
    "    title: str\n"
    "class Item(ItemBase, table=True):\n"
    "    id: int\n"
    "class Hero(SQLModel, table=True):\n"
    '    __tablename__ = "heroes"\n'
    "class ItemPublic(ItemBase):\n"
    "    pass\n"
    "class Other(Unknown, table=True):\n"
    "    pass\n",
    # A migration's frozen copy of a model of the same name.
    "app/migrations/v1.py": "from sqlalchemy.orm import declarative_base\n"
    "Base = declarative_base()\n"
    "class Item(Base):\n"
    '    __tablename__ = "old_items"\n',
    "app/routes.py": "from sqlmodel import select\n"
    "from .models import Item, Hero\n"
    "def list_items(session):\n"
    "    return session.exec(select(Item)).all()\n"
    "def one(session, key):\n"
    "    return session.get(Item, key)\n"
    "def heroes():\n"
    "    return Hero.query.all()\n"
    "def missing(session):\n"
    "    return session.get(Missing, 1)\n"
    "def create(session):\n"
    "    hero = Hero()\n"
    "    session.add(hero)\n"
    "    session.refresh(hero)\n"
    "def other(cache):\n"
    "    item = Item()\n"
    "    cache.add(item)\n"
    "def untyped(session, x):\n"
    "    session.delete(x)\n"
    "Dep = Annotated[Hero, object()]\n"
    "def typed(session, hero: Dep):\n"
    "    session.add(hero)\n"
    "def fetched(session, key):\n"
    "    item = session.get(Item, key)\n"
    "    session.delete(item)\n"
    "def made() -> Hero | None:\n"
    "    return None\n"
    "def from_call(session):\n"
    "    hero = made()\n"
    "    session.refresh(hero)\n",
}


@unittest.skipUnless(PYTHON, "Provision the python grammar")
class SqlModelTests(unittest.TestCase):
    def analyze(self, with_index):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, SQLMODEL)
            indexes = (self.index(directory),) if with_index else ()
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["python"], scip_indexes=indexes
                )
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
        )
        return edges, output.metadata.custom_metrics

    def index(self, directory):
        """A scip-python index of SQLMODEL's names, ingested."""
        import deepthought
        from deepthought._scip import scip_pb2 as wire

        package = "scip-python python app 0 "
        models = package + "`app.models`/"
        routes = package + "`app.routes`/"
        session = (
            "scip-python python SQLAlchemy 2.0.0 `sqlalchemy.orm.session`/Session#"
        )
        documents = {
            "app/models.py": [
                (1, "ItemBase", models + "ItemBase#", True),
                (3, "Item", models + "Item#", True),
                (3, "ItemBase", models + "ItemBase#", False),
                (5, "Hero", models + "Hero#", True),
            ],
            "app/migrations/v1.py": [
                (2, "Item", package + "`app.migrations.v1`/Item#", True),
            ],
            "app/routes.py": [
                (3, "Item", models + "Item#", False),
                (5, "Item", models + "Item#", False),
                (7, "Hero", models + "Hero#", False),
                (11, "hero", "local 0", True),
                (11, "Hero", models + "Hero#", False),
                (12, "add", session + "add().", False),
                (12, "hero", "local 0", False),
                (13, "refresh", session + "refresh().", False),
                (13, "hero", "local 0", False),
                (15, "item", "local 1", True),
                (
                    16,
                    "add",
                    "scip-python python stdlib 3.11 `builtins`/set#add().",
                    False,
                ),
                (16, "item", "local 1", False),
                (18, "delete", session + "delete().", False),
                (18, "x", package + "`app.routes`/untyped().(x)", False),
                (19, "Dep", routes + "Dep.", True),
                (
                    19,
                    "Annotated",
                    "scip-python python stdlib 3.11 typing/Annotated.",
                    False,
                ),
                (19, "Hero", models + "Hero#", False),
                (20, "hero", routes + "typed().(hero)", True),
                (20, "Dep", routes + "Dep.", False),
                (21, "add", session + "add().", False),
                (21, "hero", routes + "typed().(hero)", False),
                (23, "item", "local 2", True),
                (23, "get", session + "get().", False),
                (23, "Item", models + "Item#", False),
                (24, "delete", session + "delete().", False),
                (24, "item", "local 2", False),
                (25, "made", routes + "made().", True),
                (25, "Hero", models + "Hero#", False),
                (28, "hero", "local 3", True),
                (28, "made", routes + "made().", False),
                (29, "refresh", session + "refresh().", False),
                (29, "hero", "local 3", False),
            ],
        }
        # scip-python documents locals with the type pyright infers.
        types = {"local 0": "app.models.Hero", "local 1": "app.models.Item"}
        index = wire.Index()
        index.metadata.tool_info.name = "scip-python"
        index.metadata.tool_info.version = "0.6.8"
        for path, occurrences in documents.items():
            lines = SQLMODEL[path].split("\n")
            document = index.documents.add(relative_path=path)
            if path == "app/routes.py":
                for local, qualified in types.items():
                    document.symbols.add(
                        symbol=local, documentation=[f"```python\n{qualified}\n```"]
                    )
            for line, text, symbol, definition in occurrences:
                column = lines[line].index(text)
                document.occurrences.add(
                    symbol=symbol,
                    range=[line, column, column + len(text)],
                    symbol_roles=wire.Definition if definition else 0,
                )
        path = Path(directory) / ".index.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"

    def test_names_without_an_index(self):
        edges, metrics = self.analyze(False)
        self.assertEqual(
            edges,
            [
                ("MAPS_TO", "Hero", "heroes", "sqlalchemy-tablename"),
                ("MAPS_TO", "Item", "item", "sqlmodel-default-table"),
                ("MAPS_TO", "Item", "old_items", "sqlalchemy-tablename"),
                ("READS", "heroes", "heroes", "flask-sqlalchemy-model-query"),
            ],
        )
        # select(Item) names two mapped classes; get() and instances passed
        # to a Session need an index.
        self.assertEqual(
            metrics["sqlalchemy_access_resolution"],
            {
                "linked": 1,
                "linked_scip": 0,
                "not_a_mapped_class": 4,
                "scip_not_a_mapped_class": 0,
                "instance_no_scip_index": 7,
                "instance_method_unresolved": 0,
                "instance_not_session": 0,
                "instance_type_unknown": 0,
            },
        )

    def test_names_resolved_through_a_scip_index(self):
        edges, metrics = self.analyze(True)
        self.assertEqual(
            edges,
            [
                ("MAPS_TO", "Hero", "heroes", "sqlalchemy-tablename"),
                ("MAPS_TO", "Item", "item", "sqlmodel-default-table"),
                ("MAPS_TO", "Item", "old_items", "sqlalchemy-tablename"),
                ("READS", "create", "heroes", "sqlalchemy-scip-instance"),
                ("READS", "fetched", "item", "sqlalchemy-scip-get"),
                ("READS", "from_call", "heroes", "sqlalchemy-scip-instance"),
                ("READS", "heroes", "heroes", "sqlalchemy-scip-model-query"),
                ("READS", "list_items", "item", "sqlalchemy-scip-statement"),
                ("READS", "one", "item", "sqlalchemy-scip-get"),
                ("WRITES", "create", "heroes", "sqlalchemy-scip-instance"),
                ("WRITES", "fetched", "item", "sqlalchemy-scip-instance"),
                ("WRITES", "typed", "heroes", "sqlalchemy-scip-instance"),
            ],
        )
        # Missing has no occurrence in the index and no mapped class;
        # cache.add is not a Session's, and untyped's x has no inferred or
        # declared type. typed's hero is declared through an Annotated
        # alias, fetched's item by Session.get and from_call's hero by
        # made()'s return annotation.
        self.assertEqual(
            metrics["sqlalchemy_access_resolution"],
            {
                "linked": 0,
                "linked_scip": 9,
                "not_a_mapped_class": 1,
                "scip_not_a_mapped_class": 0,
                "instance_no_scip_index": 0,
                "instance_method_unresolved": 0,
                "instance_not_session": 1,
                "instance_type_unknown": 1,
            },
        )


TYPEORM_SCIP = {
    "package.json": '{"name": "app", "dependencies": {"typeorm": "0.3.28"}}',
    "src/user.entity.ts": "import { Entity } from 'typeorm';\n"
    "@Entity('users')\n"
    "export class User {}\n",
    "src/user.service.ts": "import { Repository, DataSource, EntityManager } from 'typeorm';\n"
    "import { InjectRepository } from '@nestjs/typeorm';\n"
    "import { User } from './user.entity';\n"
    "export class UserService {\n"
    "  constructor(@InjectRepository(User) private repo: Repository<User>, private ds: DataSource, private em: EntityManager) {}\n"
    "  list() { return this.repo.find(); }\n"
    "  add(u: User) { return this.repo.save(u); }\n"
    "  build() { return this.repo.create({}); }\n"
    "  count() { return this.ds.getRepository(User).count(); }\n"
    "  held() { const r = this.ds.getRepository(User); return r.delete(1); }\n"
    "  managed() { return this.em.findOneBy(User, {}); }\n"
    "  other(c: any) { return c.find(); }\n"
    "}\n"
    "export class UserRepo extends Repository<User> {\n"
    "  mine() { return this.findBy({}); }\n"
    "}\n",
    "src/builder.ts": "import { Repository, DataSource } from 'typeorm';\n"
    "import { User } from './user.entity';\n"
    "export class Builders {\n"
    "  constructor(private repo: Repository<User>, private ds: DataSource) {}\n"
    "  rows() { return this.repo.createQueryBuilder('u').where('1').getMany(); }\n"
    "  purge() { return this.ds.createQueryBuilder().delete().from(User).execute(); }\n"
    "  links() { return this.ds.createQueryBuilder().insert().into('user_links').execute(); }\n"
    "  cte(q: any) { return this.ds.createQueryBuilder().addCommonTableExpression(q, '_u').from('_u', 'x').getRawMany(); }\n"
    "  raw() { return this.repo.query('DELETE FROM sessions'); }\n"
    "  rel(u: User) { return this.repo.createQueryBuilder().relation('tags').of(u).add(1); }\n"
    "}\n",
}


DRIZZLE_SCIP = {
    "src/schema.ts": """import { pgTable, text } from "drizzle-orm/pg-core";
export const users = pgTable("users", { id: text("id") });
export const orders = pgTable("orders", { id: text("id") });
""",
    "src/repo.ts": """import { db } from "./db";
import { users, orders as o } from "@app/schema";
import * as schema from "./schema";
import { cache } from "./cache";
export async function list() {
  return db.select().from(users);
}
export async function add() {
  await db.insert(o).values({});
}
export async function joined() {
  return db.query.users.findMany();
}
export async function other() {
  return cache.from(users);
}
export async function member() {
  return db.delete(schema.orders);
}
""",
}


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_typescript"),
    "Provision the typescript grammar",
)
class DrizzleScipTests(unittest.TestCase):
    def analyze(self, with_index, unresolved=()):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DRIZZLE_SCIP)
            indexes = (self.index(directory, unresolved),) if with_index else ()
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["typescript"], scip_indexes=indexes
                )
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        metrics = output.metadata.custom_metrics
        return edges, metrics["drizzle_scip"], metrics["drizzle_access_resolution"]

    def index(self, directory, unresolved=()):
        """A scip-typescript index of DRIZZLE_SCIP's names: drizzle-orm's
        query builder methods, except other()'s from, and the table
        variables through the package import and the namespace. The names
        in ``unresolved`` get no symbol."""
        import re

        import deepthought
        from deepthought._scip import scip_pb2 as wire

        drizzle = "scip-typescript npm drizzle-orm 0.45.3 pg-core/"
        database = drizzle + "`db.d.ts`/PgDatabase#"
        select = drizzle + "query-builders/`select.d.ts`/PgSelectBuilder#from()."
        query = drizzle + "query-builders/`query.d.ts`/RelationalQueryBuilder#"
        schema = "scip-typescript npm app 1 src/`schema.ts`/"
        documents = {
            "src/schema.ts": [
                (1, "users", schema + "users.", True, 0),
                (2, "orders", schema + "orders.", True, 0),
            ],
            "src/repo.ts": [
                (5, "select", database + "select().", False, 0),
                (5, "from", select, False, 0),
                (5, "users", schema + "users.", False, 0),
                (8, "insert", database + "insert().", False, 0),
                (8, "o", schema + "orders.", False, 0),
                (11, "query", database + "query.", False, 0),
                (11, "users", schema + "users.", False, 0),
                (11, "findMany", query + "findMany().", False, 0),
                (
                    14,
                    "from",
                    "scip-typescript npm other 1 `c.d.ts`/C#from().",
                    False,
                    0,
                ),
                (14, "users", schema + "users.", False, 0),
                (17, "delete", database + "delete().", False, 0),
                (17, "orders", schema + "orders.", False, 0),
            ],
        }
        index = wire.Index()
        index.metadata.tool_info.name = "scip-typescript"
        index.metadata.tool_info.version = "0.6.2"
        for path, occurrences in documents.items():
            lines = DRIZZLE_SCIP[path].split("\n")
            document = index.documents.add(relative_path=path)
            for line, text, symbol, definition, nth in occurrences:
                if text in unresolved:
                    continue
                match = list(re.finditer(rf"\b{text}\b", lines[line]))[nth]
                document.occurrences.add(
                    symbol=symbol,
                    range=[line, match.start(), match.end()],
                    symbol_roles=wire.Definition if definition else 0,
                )
        path = Path(directory) / ".index.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"

    def test_uses_resolved_through_a_scip_index(self):
        edges, counts, by_name = self.analyze(True)
        # other()'s from is not drizzle-orm's: the index drops it.
        self.assertEqual(
            edges,
            [
                ("READS", "joined", "users", "drizzle-scip"),
                ("READS", "list", "users", "drizzle-scip"),
                ("WRITES", "add", "orders", "drizzle-scip"),
                ("WRITES", "member", "orders", "drizzle-scip"),
            ],
        )
        self.assertEqual(
            counts,
            {
                "linked": 4,
                "no_scip_index": 0,
                "index_unresolved": 0,
                "not_drizzle": 1,
                "not_a_table": 0,
            },
        )
        self.assertEqual(by_name, {"linked": 0, "not_a_table": 0})

    def test_uses_the_index_could_not_type_are_counted(self):
        # No symbol at findMany: the index read nothing there, and the name
        # rule reads that use.
        edges, counts, by_name = self.analyze(True, unresolved=("findMany",))
        self.assertEqual(counts["index_unresolved"], 1)
        self.assertIn(("READS", "joined", "users", "drizzle-query"), edges)
        self.assertEqual(by_name["linked"], 1)

    def test_without_an_index_the_name_rule_reads_uses(self):
        edges, counts, by_name = self.analyze(False)
        # The name rule does not check the receiver: other() reads users.
        self.assertIn(("READS", "other", "users", "drizzle-query"), edges)
        self.assertEqual(counts["no_scip_index"], 5)
        self.assertEqual(by_name, {"linked": 5, "not_a_table": 0})


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_typescript"),
    "Provision the typescript grammar",
)
class TypeormScipTests(unittest.TestCase):
    def analyze(self, with_index, unresolved=()):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, TYPEORM_SCIP)
            indexes = (self.index(directory, unresolved),) if with_index else ()
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["typescript"], scip_indexes=indexes
                )
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        metrics = output.metadata.custom_metrics
        return edges, metrics["typeorm_scip"], metrics["orm_access_resolution"]

    def index(self, directory, unresolved=()):
        """A scip-typescript index of TYPEORM_SCIP's names: TypeORM's
        repository, data source and entity manager methods, except
        other()'s find. The names in ``unresolved`` get no symbol, as in a
        file whose imports the indexer could not follow."""
        import re

        import deepthought
        from deepthought._scip import scip_pb2 as wire

        typeorm = "scip-typescript npm typeorm 0.3.28 "
        repository = typeorm + "repository/`Repository.d.ts`/Repository#"
        source = typeorm + "data-source/`DataSource.d.ts`/DataSource#getRepository()."
        manager = typeorm + "entity-manager/`EntityManager.d.ts`/EntityManager#"
        nest = (
            "scip-typescript npm @nestjs/typeorm 11.0.1 "
            "dist/common/`typeorm.decorators.d.ts`/InjectRepository."
        )
        app = "scip-typescript npm app 1 src/"
        user = app + "`user.entity.ts`/User#"
        service = app + "`user.service.ts`/UserService#"
        builders = app + "`builder.ts`/Builders#"
        source_builder = (
            typeorm + "data-source/`DataSource.d.ts`/DataSource#createQueryBuilder()."
        )

        def qb(method):
            return (
                typeorm
                + "query-builder/`SelectQueryBuilder.d.ts`/SelectQueryBuilder#"
                + method
                + "()."
            )

        documents = {
            "src/builder.ts": [
                (1, "User", user, False, 0),
                (3, "repo", builders + "repo.", True, 0),
                (3, "Repository", repository, False, 0),
                (3, "User", user, False, 0),
                (3, "ds", builders + "ds.", True, 0),
                (4, "repo", builders + "repo.", False, 0),
                (
                    4,
                    "createQueryBuilder",
                    repository + "createQueryBuilder().",
                    False,
                    0,
                ),
                (4, "where", qb("where"), False, 0),
                (4, "getMany", qb("getMany"), False, 0),
                (5, "ds", builders + "ds.", False, 0),
                (5, "createQueryBuilder", source_builder, False, 0),
                (5, "delete", qb("delete"), False, 0),
                (5, "from", qb("from"), False, 0),
                (5, "User", user, False, 0),
                (6, "ds", builders + "ds.", False, 0),
                (6, "createQueryBuilder", source_builder, False, 0),
                (6, "insert", qb("insert"), False, 0),
                (6, "into", qb("into"), False, 0),
                (7, "ds", builders + "ds.", False, 0),
                (7, "createQueryBuilder", source_builder, False, 0),
                (
                    7,
                    "addCommonTableExpression",
                    qb("addCommonTableExpression"),
                    False,
                    0,
                ),
                (7, "from", qb("from"), False, 0),
                (8, "repo", builders + "repo.", False, 0),
                (8, "query", repository + "query().", False, 0),
                (9, "repo", builders + "repo.", False, 0),
                (
                    9,
                    "createQueryBuilder",
                    repository + "createQueryBuilder().",
                    False,
                    0,
                ),
                (9, "relation", qb("relation"), False, 0),
            ],
            "src/user.entity.ts": [(2, "User", user, True, 0)],
            "src/user.service.ts": [
                (2, "User", user, False, 0),
                (4, "InjectRepository", nest, False, 0),
                (4, "User", user, False, 0),
                (4, "repo", service + "repo.", True, 0),
                (4, "Repository", repository, False, 0),
                (4, "User", user, False, 1),
                (4, "ds", service + "ds.", True, 0),
                (4, "em", service + "em.", True, 0),
                (5, "repo", service + "repo.", False, 0),
                (5, "find", repository + "find().", False, 0),
                (6, "repo", service + "repo.", False, 0),
                (6, "save", repository + "save().", False, 0),
                (8, "ds", service + "ds.", False, 0),
                (8, "getRepository", source, False, 0),
                (8, "User", user, False, 0),
                (8, "count", repository + "count().", False, 1),
                (9, "r", "local 1", True, 0),
                (9, "ds", service + "ds.", False, 0),
                (9, "getRepository", source, False, 0),
                (9, "User", user, False, 0),
                (9, "r", "local 1", False, 1),
                (9, "delete", repository + "delete().", False, 0),
                (10, "em", service + "em.", False, 0),
                (10, "findOneBy", manager + "findOneBy().", False, 0),
                (10, "User", user, False, 0),
                (
                    11,
                    "find",
                    "scip-typescript npm other 1 `c.d.ts`/C#find().",
                    False,
                    0,
                ),
                (13, "Repository", repository, False, 0),
                (13, "User", user, False, 0),
                (14, "findBy", repository + "findBy().", False, 0),
            ],
        }
        index = wire.Index()
        index.metadata.tool_info.name = "scip-typescript"
        index.metadata.tool_info.version = "0.6.2"
        for path, occurrences in documents.items():
            lines = TYPEORM_SCIP[path].split("\n")
            document = index.documents.add(relative_path=path)
            for line, text, symbol, definition, nth in occurrences:
                if text in unresolved:
                    continue
                match = list(re.finditer(rf"\b{text}\b", lines[line]))[nth]
                document.occurrences.add(
                    symbol=symbol,
                    range=[line, match.start(), match.end()],
                    symbol_roles=wire.Definition if definition else 0,
                )
        path = Path(directory) / ".index.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"

    def test_calls_resolved_through_a_scip_index(self):
        edges, counts, orm = self.analyze(True)
        # Each call is read where its method is written; create() touches
        # no table and other()'s find is not TypeORM's. A query builder
        # reads or writes its root table: the repository's entity, or what
        # its from/into names (a literal too), never a CTE's name; the
        # builders' own delete and insert are not repository calls.
        # A relation builder writes the related side, not its root: no edge.
        # Raw SQL given to TypeORM's query is read as SQL.
        self.assertEqual(
            edges,
            [
                ("READS", "Builders.rows", "users", "typeorm-scip"),
                ("READS", "UserRepo.mine", "users", "typeorm-scip"),
                ("READS", "UserService.count", "users", "typeorm-scip"),
                ("READS", "UserService.list", "users", "typeorm-scip"),
                ("READS", "UserService.managed", "users", "typeorm-scip"),
                ("WRITES", "Builders.links", "user_links", "typeorm-scip"),
                ("WRITES", "Builders.purge", "users", "typeorm-scip"),
                ("WRITES", "Builders.raw", "sessions", "typeorm-sql"),
                ("WRITES", "UserService.add", "users", "typeorm-scip"),
                ("WRITES", "UserService.held", "users", "typeorm-scip"),
            ],
        )
        self.assertEqual(
            counts,
            {
                "linked": 9,
                "no_scip_index": 0,
                "not_typeorm": 3,
                "entity_unknown": 1,
                "not_an_entity": 0,
                "builders": 3,
                "relation_builders": 1,
            },
        )
        # The decorator and getRepository name rule gives way in the file.
        self.assertEqual(orm["typeorm_name_rule_replaced"], 3)
        self.assertEqual(orm["typeorm_linked"], 0)

    def test_calls_the_index_could_not_type_are_counted(self):
        # Without symbols at the repository's names, the index read nothing
        # there: those uses are counted, and the name rule reads them.
        edges, counts, orm = self.analyze(
            True, unresolved=("InjectRepository", "getRepository")
        )
        self.assertEqual(orm["typeorm_index_unresolved"], 3)
        self.assertEqual(orm["typeorm_name_rule_replaced"], 0)
        self.assertIn(
            ("READS", "UserService.constructor", "users", "typeorm-repository"),
            edges,
        )

    def test_without_an_index_the_name_rule_reads_repositories(self):
        edges, counts, orm = self.analyze(False)
        self.assertEqual(
            edges,
            [
                ("READS", "UserService.constructor", "users", "typeorm-repository"),
                ("READS", "UserService.count", "users", "typeorm-repository"),
                ("READS", "UserService.held", "users", "typeorm-repository"),
                ("WRITES", "UserService.constructor", "users", "typeorm-repository"),
                ("WRITES", "UserService.held", "users", "typeorm-repository"),
            ],
        )
        self.assertEqual(counts["no_scip_index"], 14)
        self.assertEqual(orm["typeorm_linked"], 3)
        self.assertEqual(orm["typeorm_name_rule_replaced"], 0)


KYSELY = {
    "package.json": '{"name": "app", "dependencies": {"kysely": "0.29.8"}}',
    "src/repo.ts": "import { Kysely } from 'kysely';\n"
    "export class Repo {\n"
    "  constructor(private db: Kysely<any>) {}\n"
    "  list() { return this.db.selectFrom('asset as a').innerJoin('user', 'user.id', 'a.ownerId').execute(); }\n"
    "  add(v: any) { return this.db.insertInto('album').values(v).execute(); }\n"
    "  cte() { return this.db.with('asset', (qb) => qb.selectFrom('asset')).selectFrom('asset').execute(); }\n"
    "  rec() { return this.db.withRecursive('tree', (qb) => qb.selectFrom('tag').union(qb.selectFrom('tree'))).selectFrom('tree').execute(); }\n"
    "  shadow() { return this.db.with('album', (d) => d.insertInto('album').values({})).selectFrom('album').execute(); }\n"
    "  many() { return this.db.selectFrom(['person', 'pet']).execute(); }\n"
    "}\n",
    # Not kysely: no import, and in the index another library's method.
    "src/other.ts": "export const other = (client: any) => client.selectFrom('nope');\n",
    # tree-sitter cannot parse the tagged template's object type argument.
    "src/partial.ts": "import { sql } from 'kysely';\n"
    "export function f(db: any) {\n"
    "  return db.selectFrom('face').select(sql<{ a: string }>`x`.as('a'));\n"
    "}\n",
}


@unittest.skipUnless(
    importlib.util.find_spec("tree_sitter")
    and importlib.util.find_spec("tree_sitter_typescript"),
    "Provision the typescript grammar",
)
class KyselyTests(unittest.TestCase):
    def analyze(self, with_index, metrics=False):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, KYSELY)
            indexes = (self.index(directory),) if with_index else ()
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["typescript"], scip_indexes=indexes
                )
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        found = output.metadata.custom_metrics
        if metrics:
            return edges, found["kysely"], found
        return edges, found["kysely"]

    def index(self, directory):
        """A scip-typescript index whose method references are Kysely's,
        except add()'s insertInto and other.ts's selectFrom."""
        import deepthought
        from deepthought._scip import scip_pb2 as wire

        kysely = "scip-typescript npm kysely 0.29.8 dist/`query-creator.d.ts`/"
        builder = (
            "scip-typescript npm kysely 0.29.8 "
            "dist/query-builder/`select-query-builder.d.ts`/SelectQueryBuilder#"
        )
        documents = {
            "src/repo.ts": [
                (3, "selectFrom", kysely + "QueryCreator#selectFrom().", 0),
                (3, "innerJoin", builder + "innerJoin().", 0),
                (
                    4,
                    "insertInto",
                    "scip-typescript npm app 1 src/`fake.ts`/Fake#insertInto().",
                    0,
                ),
                (5, "with", kysely + "QueryCreator#with().", 0),
                (5, "selectFrom", kysely + "QueryCreator#selectFrom().", 0),
                (5, "selectFrom", kysely + "QueryCreator#selectFrom().", 1),
                (6, "withRecursive", kysely + "QueryCreator#withRecursive().", 0),
                (6, "selectFrom", kysely + "QueryCreator#selectFrom().", 0),
                (6, "selectFrom", kysely + "QueryCreator#selectFrom().", 1),
                (6, "selectFrom", kysely + "QueryCreator#selectFrom().", 2),
                (7, "with", kysely + "QueryCreator#with().", 0),
                (7, "insertInto", kysely + "QueryCreator#insertInto().", 0),
                (7, "selectFrom", kysely + "QueryCreator#selectFrom().", 0),
                (8, "selectFrom", kysely + "QueryCreator#selectFrom().", 0),
            ],
            "src/other.ts": [
                (
                    0,
                    "selectFrom",
                    "scip-typescript npm other 2 Client#selectFrom().",
                    0,
                ),
            ],
            "src/partial.ts": [
                (2, "selectFrom", kysely + "QueryCreator#selectFrom().", 0),
            ],
        }
        index = wire.Index()
        index.metadata.tool_info.name = "scip-typescript"
        index.metadata.tool_info.version = "0.6.2"
        for path, occurrences in documents.items():
            lines = KYSELY[path].split("\n")
            document = index.documents.add(relative_path=path)
            for line, text, symbol, nth in occurrences:
                column = -1
                for _ in range(nth + 1):
                    column = lines[line].index("." + text + "(", column + 1)
                document.occurrences.add(
                    symbol=symbol, range=[line, column + 1, column + 1 + len(text)]
                )
        path = Path(directory) / ".index.scip"
        path.write_bytes(index.SerializeToString())
        return deepthought.ingest_scip(path, repository_id="r"), "sha256:test"

    def test_without_an_index_kysely_is_not_read_and_the_run_says_so(self):
        edges, counts, metrics = self.analyze(False, metrics=True)
        self.assertEqual(edges, [])
        # repo.ts and partial.ts (repaired); other.ts does not import kysely.
        self.assertEqual(counts, {"calls_scip": 0, "not_kysely": 0, "no_scip_index": 8})
        self.assertEqual(metrics["analysis_status"], "partial")

    def test_methods_resolved_through_a_scip_index(self):
        edges, counts = self.analyze(True)
        self.assertEqual(
            edges,
            [
                ("READS", "Repo.cte", "asset", "kysely-scip"),
                ("READS", "Repo.list", "asset", "kysely-scip"),
                ("READS", "Repo.list", "user", "kysely-scip"),
                ("READS", "Repo.many", "person", "kysely-scip"),
                ("READS", "Repo.many", "pet", "kysely-scip"),
                ("READS", "Repo.rec", "tag", "kysely-scip"),
                ("READS", "f", "face", "kysely-scip"),
                ("WRITES", "Repo.shadow", "album", "kysely-scip"),
            ],
        )
        self.assertEqual(counts, {"calls_scip": 7, "not_kysely": 2, "no_scip_index": 0})


class StatementTests(unittest.TestCase):
    def test_readers(self):
        # sqlglot reads SQL; the token reader only what no parser accepts.
        read = statement_tables("SELECT * FROM pets p JOIN visits v ON p.id = v.pet")
        self.assertEqual(
            (read.reads, read.writes, read.reader), (("pets", "visits"), (), "sqlglot")
        )
        held = statement_tables("SELECT * FROM \x00 JOIN visits v ON true")
        self.assertEqual((held.reads, held.reader), (("visits",), "tokens"))
        jpql = statement_tables("SELECT o FROM Owner o", jpql=True)
        self.assertEqual((jpql.reads, jpql.reader), (("Owner",), "tokens"))
        # SQL no dialect accepts is read by tokens, and says so.
        bad = statement_tables("UPDATE owners SET city = 1 WHERE id = ? FOR UPDATE")
        self.assertEqual(
            (bad.reads, bad.writes, bad.reader), ((), ("owners",), "tokens_not_parsed")
        )
        locked = statement_tables(
            "SELECT e.* FROM events e JOIN handled he ON true FOR UPDATE OF he"
        )
        self.assertEqual(locked.reads, ("events", "handled"))
        # Positional parameters, and a multi-table DELETE's aliases.
        positional = statement_tables("DELETE FROM visits WHERE pet_id = ?1")
        self.assertEqual(positional.writes, ("visits",))
        aliased = statement_tables(
            "DELETE i, c FROM items i JOIN cache c ON c.id = i.id WHERE c.t < 1"
        )
        self.assertEqual(sorted(aliased.writes), ["cache", "items"])

    def test_tables_read_and_written(self):
        cases = {
            "SELECT p.id FROM pets p LEFT OUTER JOIN visits v ON p.id = v.pet": (
                ("pets", "visits"),
                (),
            ),
            "INSERT INTO visits (a, b) SELECT a, b FROM staged.Visits": (
                ("visits",),
                ("visits",),
            ),
            "UPDATE owners SET city = 'FROM x' WHERE id = ?": (
                (),
                ("owners",),
            ),
            "DELETE FROM pets WHERE id IN (SELECT pet FROM visits)": (
                ("visits",),
                ("pets",),
            ),
            # System catalogs are not the application's tables.
            "SELECT table_name FROM INFORMATION_SCHEMA.TABLES t "
            "JOIN pg_catalog.pg_class c ON true JOIN pg_attribute a ON true "
            "JOIN users u ON true": (("users",), ()),
            "SELECT name FROM sqlite_master UNION SELECT name FROM sys.databases "
            "UNION SELECT table_name FROM all_tables UNION SELECT mview_name FROM user_mviews "
            "UNION SELECT name FROM user_settings": (("user_settings",), ()),
            "WITH recent (id) AS (SELECT id FROM visits) SELECT * FROM recent": (
                ("visits",),
                (),
            ),
            "SELECT EXTRACT(YEAR FROM born) FROM pets, owners o -- FROM vets": (
                ("pets", "owners"),
                (),
            ),
            "MERGE INTO stock s USING deliveries d ON s.id = d.id "
            "WHEN MATCHED THEN UPDATE SET qty = d.qty": (
                ("deliveries",),
                ("stock",),
            ),
            "SELECT * FROM generate_series(1, 3), dual": ((), ()),
            "SELECT * FROM audit_\x00": ((), ()),
            "TRUNCATE TABLE audit_log": ((), ("audit_log",)),
        }
        for text, (reads, writes) in cases.items():
            with self.subTest(text=text):
                statement = statement_tables(text)
                self.assertEqual((statement.reads, statement.writes), (reads, writes))

    def test_jpql_names_entities_not_paths(self):
        statement = statement_tables(
            "FROM Owner o LEFT JOIN FETCH o.pets JOIN Vet v ON v.city = o.city",
            jpql=True,
        )
        self.assertEqual(statement.reads, ("Owner", "Vet"))
        self.assertEqual(
            statement_tables("UPDATE Owner o SET o.city = :c", jpql=True).writes,
            ("Owner",),
        )

    def test_java_text_blocks(self):
        self.assertEqual(
            _text_block('"""\n    SELECT a\n      FROM b  \n    """'),
            "SELECT a\n  FROM b\n",
        )
        self.assertEqual(_text_block('"""\n  a \\\n  b\\s"""'), "a b ")


JAVA_SQL = {
    "pom.xml": "<project><artifactId>shop</artifactId></project>",
    "src/main/java/shop/Owner.java": """package shop;
import jakarta.persistence.*;
@Entity
@Table(name = "owners")
public class Owner { @Id Integer id; }
""",
    "src/main/java/shop/PetType.java": """package shop;
import jakarta.persistence.Entity;
@Entity(name = "Kind")
public class PetType { Integer id; }
""",
    "src/main/java/shop/OwnerRepository.java": """package shop;
import java.util.List;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.Repository;
public interface OwnerRepository extends Repository<Owner, Integer> {
    @Query("SELECT o FROM Owner o WHERE o.city = :city")
    List<Owner> inCity(String city);
    @Query("FROM Kind k ORDER BY k.name")
    List<PetType> kinds();
    @Query(value = "DELETE FROM visits WHERE pet_id = ?1", nativeQuery = true)
    void forget(int pet);
    @Query("SELECT u FROM Unknown u")
    List<Object> unknown();
}
""",
    "src/main/java/shop/JdbcStore.java": """package shop;
import jakarta.persistence.EntityManager;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.jdbc.core.simple.SimpleJdbcInsert;
public class JdbcStore {
    private static final String VISITS = "visits";
    private final JdbcTemplate jdbcTemplate;
    private final JdbcClient client;
    private final SimpleJdbcInsert insert;
    private EntityManager em;
    JdbcStore(JdbcTemplate jdbcTemplate, JdbcClient client) {
        this.jdbcTemplate = jdbcTemplate;
        this.client = client;
        this.insert = new SimpleJdbcInsert(jdbcTemplate).withTableName("pets");
    }
    int count() {
        return jdbcTemplate.queryForObject("SELECT count(*) FROM " + VISITS, Integer.class);
    }
    void rename(String table) {
        jdbcTemplate.update("UPDATE " + table + " SET name = ?", "x");
    }
    Object owners() {
        return client.sql(\"\"\"
            SELECT o.id
            FROM owners o
            JOIN pets p ON p.owner_id = o.id
            \"\"\").query(Object.class).list();
    }
    Object owner() {
        return em.createQuery("SELECT o FROM Owner o").getResultList();
    }
}
""",
}


@unittest.skipUnless(JAVA, "Install the java grammar")
class JavaSqlTests(unittest.TestCase):
    def test_queries_jdbc_and_entity_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, JAVA_SQL)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["java"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        self.assertEqual(
            edges,
            [
                ("READS", "JdbcStore.count", "visits", "jdbc-sql"),
                ("READS", "JdbcStore.owner", "owners", "jpa-query"),
                ("READS", "JdbcStore.owners", "owners", "jdbc-sql"),
                ("READS", "JdbcStore.owners", "pets", "jdbc-sql"),
                ("READS", "OwnerRepository.inCity", "owners", "spring-data-query"),
                ("READS", "OwnerRepository.kinds", "kind", "spring-data-query"),
                ("WRITES", "JdbcStore.JdbcStore", "pets", "jdbc-insert-table"),
                ("WRITES", "OwnerRepository.forget", "visits", "spring-data-query"),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["sql_text"],
            {
                "statements": 9,
                "not_fixed": 1,
                "unknown_entity": 1,
                "sqlglot": 4,
                "tokens": 4,
                "tokens_not_parsed": 0,
            },
        )
        for relationship in output.relationships:
            self.assertIsNotNone(get_rule(relationship.properties["rule_id"]))


ENTITY_MANAGER = {
    "pom.xml": "<project><artifactId>shop</artifactId></project>",
    "src/main/java/shop/Owner.java": JAVA_SQL["src/main/java/shop/Owner.java"],
    "src/main/java/shop/OwnerStore.java": """package shop;
import java.util.List;
import jakarta.persistence.EntityManager;
public class OwnerStore {
    private EntityManager em;
    private Owner current;
    private List<Owner> recent;
    void save(Owner owner) {
        if (owner.id == null) { em.persist(owner); } else { this.em.merge(owner); }
    }
    Owner load(int id) { return em.find(Owner.class, id); }
    void forgetCurrent() { em.remove(this.current); }
    void forgetRecent(Owner owner) { recent.remove(owner); }
    void unknown(Object thing) { em.remove(thing); }
}
""",
}


@unittest.skipUnless(JAVA, "Install the java grammar")
class EntityManagerTests(unittest.TestCase):
    def test_calls_on_declared_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, ENTITY_MANAGER)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["java"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.properties["line"],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
            )
            for r in output.relationships
            if r.properties["rule_id"] == "data-contracts.jpa-entity-manager"
        )
        self.assertEqual(
            edges,
            [
                ("READS", "OwnerStore.load", 11, "owners"),
                ("WRITES", "OwnerStore.forgetCurrent", 12, "owners"),
                ("WRITES", "OwnerStore.save", 9, "owners"),
                ("WRITES", "OwnerStore.save", 9, "owners"),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["entity_manager_resolution"],
            {"linked": 4, "not_an_entity": 1},
        )


PYTHON_SQL = {
    "pyproject.toml": '[project]\nname = "jobs"\n',
    "jobs/store.py": """import sqlite3
import sqlalchemy as sa
from sqlalchemy import text

def pending(conn):
    return conn.execute(
        "SELECT j.id FROM jobs j "
        "JOIN workers w ON w.id = j.worker WHERE w.name = %s", ["a"]
    )

def finish(session, job):
    session.execute(sa.text("UPDATE jobs SET done = 1 WHERE id = :id"), {"id": job})

def archive(conn, day):
    conn.execute(text(f"INSERT INTO archive SELECT * FROM jobs WHERE day = {day}"))

def drop(conn, table):
    conn.execute("DELETE FROM %s" % table)

def ping(conn):
    conn.execute("SELECT 1")

conn = sqlite3.connect("jobs.db")
conn.executescript("DELETE FROM workers")
""",
    "jobs/other.py": """def run(client):
    client.execute("SELECT * FROM unrelated")
""",
}


@unittest.skipUnless(PYTHON, "Install the python grammar")
class PythonSqlTests(unittest.TestCase):
    def test_execute_calls_in_database_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, PYTHON_SQL)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
            )
            for r in output.relationships
            if r.properties["rule_id"] == "data-contracts.python-sql"
        )
        self.assertEqual(
            edges,
            [
                ("READS", "archive", "jobs"),
                ("READS", "pending", "jobs"),
                ("READS", "pending", "workers"),
                ("WRITES", "archive", "archive"),
                ("WRITES", "finish", "jobs"),
                ("WRITES", "jobs/store.py", "workers"),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["sql_text"],
            {
                "statements": 6,
                "not_fixed": 1,
                "unknown_entity": 0,
                "sqlglot": 4,
                "tokens": 1,
                "tokens_not_parsed": 0,
            },
        )


TYPESCRIPT = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)
SCRIPT_ORMS = {
    "package.json": '{"name": "shop", "devDependencies": {"prisma": "6.19.0"}, '
    '"dependencies": {"typeorm": "0.3.28"}}',
    "prisma/schema.prisma": """// The shop's tables.
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

model User {
  id      Int     @id @default(autoincrement())
  profile UserProfile?
  @@map("users")
}

/// model Ghost {} is only a comment.
model UserProfile {
  id     Int  @id
  userId Int  @unique
  user   User @relation(fields: [userId], references: [id])
}
""",
    "src/users.ts": """import { PrismaClient } from "@prisma/client";
const prisma = new PrismaClient();

export async function listUsers() {
  return prisma.user.findMany({ include: { profile: true } });
}

export async function rename(id: number, name: string) {
  await prisma.userProfile.upsert({ where: { id }, create: {}, update: {} });
  return prisma.user.update({ where: { id }, data: { name } });
}

export function other(cache: any) {
  return cache.user.get(1);
}
""",
    "src/entities.ts": """import { Entity as Table, PrimaryGeneratedColumn } from "typeorm";

@Table({ name: "audit_events" })
export class AuditEvent {
  @PrimaryGeneratedColumn() id: number;
}

export @Table() class HTTPRequestLog {}

@Table(tableName) class Dynamic {}
""",
    "src/audit.ts": """import { Injectable } from "@nestjs/common";
import { InjectRepository } from "@nestjs/typeorm";
import { DataSource, Repository } from "typeorm";
import { AuditEvent, HTTPRequestLog } from "./entities";

@Injectable()
export class AuditService {
  constructor(
    @InjectRepository(AuditEvent) private events: Repository<AuditEvent>,
    private source: DataSource,
  ) {}

  logs() {
    return this.source.getRepository(HTTPRequestLog).find();
  }

  unknown() {
    return this.source.getRepository(Missing);
  }
}
""",
}


def _npm_registry():
    """node and npm, and the npm registry reachable: the engine fetches the
    prisma-schema-wasm build a schema's Prisma pins. The acceptance sandbox
    has no network; the database job, which has, runs these tests."""
    npm = shutil.which("npm")
    if not (shutil.which("node") and npm):
        return False
    try:
        return (
            subprocess.run(
                [npm, "view", "prisma@6.19.0", "version"],
                capture_output=True,
                timeout=60,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


NODE = _npm_registry()


def _go_proxy():
    """go, and the module proxy reachable: the engine builds GORM's own
    NamingStrategy against the gorm release a module pins. The acceptance
    sandbox has no network; the graph-port job, which has, runs these
    tests."""
    go = shutil.which("go")
    if not go:
        return False
    try:
        return (
            subprocess.run(
                [go, "mod", "download", "gorm.io/gorm@v1.25.12"],
                capture_output=True,
                timeout=120,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


GORM_RELEASES = _go_proxy()


@unittest.skipUnless(TYPESCRIPT, "Install the typescript grammar")
class ScriptOrmTests(unittest.TestCase):
    @unittest.skipUnless(
        NODE, "Prisma schemas are read with node, npm and npm's registry"
    )
    def test_prisma_and_typeorm(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, SCRIPT_ORMS)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            edges,
            [
                ("DECLARES", "prisma/schema.prisma", "userprofile", "prisma-model"),
                ("DECLARES", "prisma/schema.prisma", "users", "prisma-model"),
                (
                    "MAPS_TO",
                    "AuditEvent",
                    "audit_events",
                    "typeorm-entity-name",
                ),
                (
                    "MAPS_TO",
                    "HTTPRequestLog",
                    "http_request_log",
                    "typeorm-default-table-name",
                ),
                (
                    "READS",
                    "AuditService.constructor",
                    "audit_events",
                    "typeorm-repository",
                ),
                (
                    "READS",
                    "AuditService.logs",
                    "http_request_log",
                    "typeorm-repository",
                ),
                ("READS", "listUsers", "users", "prisma-client"),
                # The profile's relation holds the foreign key to users.
                ("REFERENCES_TABLE", "db-table", "users", "prisma-foreign-key"),
                (
                    "WRITES",
                    "AuditService.constructor",
                    "audit_events",
                    "typeorm-repository",
                ),
                ("WRITES", "rename", "userprofile", "prisma-client"),
                ("WRITES", "rename", "users", "prisma-client"),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            (metrics["prisma_models"], metrics["typeorm_entities"]), (2, 2)
        )
        self.assertEqual(
            metrics["prisma_schema"],
            {
                "engines": {
                    "prisma/schema.prisma": (
                        "6.19.0-26.2ba551f319ab1df4bc874a89965d8b3641056773"
                    )
                },
                "unread": 0,
            },
        )
        self.assertEqual(
            metrics["orm_access_resolution"],
            {
                "prisma_linked": 3,
                "prisma_ambiguous": 0,
                "typeorm_linked": 2,
                "typeorm_not_an_entity": 1,
                "typeorm_wrapper_not_an_entity": 0,
                "typeorm_name_rule_replaced": 0,
                "typeorm_index_unresolved": 0,
            },
        )
        for relationship in output.relationships:
            self.assertIsNotNone(get_rule(relationship.properties["rule_id"]))

    def test_typeorm_release_of_an_entity(self):
        from deepthought.typeorm_naming import project_release

        with tempfile.TemporaryDirectory() as directory:
            write(
                directory,
                {
                    "package.json": '{"dependencies": {"typeorm": "^0.3.0"}}',
                    "api/package.json": '{"dependencies": {"typeorm": "^0.2.0"}}',
                    "api/src/a.ts": "",
                    "web/src/b.ts": "",
                    "web/yarn.lock": 'typeorm@^0.3.0:\n  version "0.3.17"\n',
                    # NestJS projects lock @nestjs/typeorm next to typeorm.
                    "pnpm/src/d.ts": "",
                    "pnpm/package.json": '{"dependencies": {"typeorm": "^0.3.17"}}',
                    "pnpm/pnpm-lock.yaml": "lockfileVersion: '9.0'\npackages:\n"
                    "  '@nestjs/typeorm@10.0.2':\n    resolution: {}\n"
                    "  typeorm@0.3.20:\n    resolution: {}\nsnapshots:\n"
                    "  '@nestjs/typeorm@10.0.2(typeorm@0.3.20(pg@8.11.3))':\n"
                    "    dependencies: {}\n",
                    "pnpm8/src/e.ts": "",
                    "pnpm8/pnpm-lock.yaml": "packages:\n"
                    "  /@nestjs/typeorm@10.0.2:\n    resolution: {}\n"
                    "  /typeorm@0.3.19:\n    resolution: {}\n",
                    "bun/src/f.ts": "",
                    "bun/bun.lock": '{"packages": {"@nestjs/typeorm": '
                    '["@nestjs/typeorm@10.0.2", ""], "typeorm": ["typeorm@0.3.18", ""]}}',
                    "cli/src/c.ts": "",
                    "cli/node_modules/typeorm/package.json": '{"version": "0.3.20"}',
                    "cli/package-lock.json": '{"packages": {"node_modules/typeorm": '
                    '{"version": "0.3.1"}}}',
                },
            )
            # The nearest declared range, when nothing is locked or installed.
            self.assertEqual(
                project_release(directory, "api/src/a.ts"), ("range", "^0.2.0")
            )
            self.assertEqual(
                project_release(directory, "web/src/b.ts"), ("release", "0.3.17")
            )
            for file, release in (
                ("pnpm/src/d.ts", "0.3.20"),
                ("pnpm8/src/e.ts", "0.3.19"),
                ("bun/src/f.ts", "0.3.18"),
            ):
                self.assertEqual(
                    project_release(directory, file), ("release", release), file
                )
            # Installed before locked, in the same directory.
            self.assertEqual(
                project_release(directory, "cli/src/c.ts"), ("release", "0.3.20")
            )

    @unittest.skipUnless(
        NODE, "TypeORM's naming strategy is installed with npm from its registry"
    )
    def test_typeorm_wrappers_and_child_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, TYPEORM_WRAPPERS)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        wrapper = "typeorm-repository-wrapper"
        self.assertEqual(
            edges,
            [
                ("MAPS_TO", "Allocation", "stock_movement", "typeorm-child-entity"),
                ("MAPS_TO", "Order", "order", "typeorm-default-table-name"),
                ("MAPS_TO", "Sale", "stock_movement", "typeorm-child-entity"),
                (
                    "MAPS_TO",
                    "StockMovement",
                    "stock_movement",
                    "typeorm-default-table-name",
                ),
                ("READS", "OrderService.count", "order", "typeorm-repository"),
                ("READS", "OrderService.find", "order", wrapper),
                ("READS", "OrderService.query", "order", wrapper),
                ("READS", "OrderService.sales", "stock_movement", wrapper),
                ("WRITES", "OrderService.query", "order", wrapper),
                ("WRITES", "OrderService.save", "order", wrapper),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            metrics["typeorm_child_entities"], {"linked": 2, "base_not_an_entity": 0}
        )
        self.assertEqual(
            metrics["orm_access_resolution"],
            {
                "prisma_linked": 0,
                "prisma_ambiguous": 0,
                "typeorm_linked": 5,
                "typeorm_not_an_entity": 0,
                "typeorm_wrapper_not_an_entity": 1,
                "typeorm_name_rule_replaced": 0,
                "typeorm_index_unresolved": 0,
            },
        )
        for relationship in output.relationships:
            self.assertIsNotNone(get_rule(relationship.properties["rule_id"]))


PRISMA_FOLDER = {
    "apps/package-lock.json": """{"packages": {
  "node_modules/@prisma/engines-version": {
    "version": "6.19.0-26.2ba551f319ab1df4bc874a89965d8b3641056773"
  }
}}""",
    "apps/api/package.json": '{"dependencies": {"@prisma/client": "^5.0.0"}}',
    # A multi-file schema: the datasource file's folder holds the models.
    "apps/api/prisma/schema/main.prisma": """datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}
""",
    "apps/api/prisma/schema/models/post.prisma": """model Post {
  id       Int    @id
  authorId Int    @map("author_id")
  author   Author @relation(fields: [authorId], references: [id], onDelete: Cascade)
  @@map(name: "posts")
}
""",
    "apps/api/prisma/schema/author.prisma": """
// model Author is declared below.
model Author {
  id    Int    @id
  posts Post[]
}
""",
    # The engine rejects this schema (a relation with no opposite field).
    "tools/broken/package.json": '{"devDependencies": {"prisma": "6.19.0"}}',
    "tools/broken/schema.prisma": """datasource db {
  provider = "sqlite"
  url      = "file:dev.db"
}

model Orphan {
  id   Int  @id
  peer Peer?
}

model Peer {
  id Int @id
}
""",
    # No Prisma version anywhere up the tree.
    "scratch/loose.prisma": """model Loose {
  id Int @id
}
""",
}


class PrismaVersionTests(unittest.TestCase):
    def test_installed_and_locked_versions_win_over_declared_ranges(self):
        from deepthought.prisma_schema import project_version

        with tempfile.TemporaryDirectory() as directory:
            write(directory, PRISMA_FOLDER)
            file = "apps/api/prisma/schema/main.prisma"
            self.assertEqual(
                project_version(directory, file),
                ("engines", "6.19.0-26.2ba551f319ab1df4bc874a89965d8b3641056773"),
            )
            os.remove(Path(directory) / "apps" / "package-lock.json")
            self.assertEqual(project_version(directory, file), ("prisma", "^5.0.0"))
            write(
                directory,
                {"apps/api/node_modules/prisma/package.json": '{"version": "5.22.0"}'},
            )
            self.assertEqual(project_version(directory, file), ("prisma", "5.22.0"))
            self.assertIsNone(project_version(directory, "scratch/loose.prisma"))
            write(
                directory,
                {"scratch/deno.json": '{"imports": {"prisma": "npm:prisma@7.10.0"}}'},
            )
            self.assertEqual(
                project_version(directory, "scratch/loose.prisma"), ("prisma", "7.10.0")
            )
            os.remove(Path(directory) / "scratch" / "deno.json")
            write(
                directory,
                {
                    "pnpm-lock.yaml": "packages:\n  '@prisma/engines-version@7.1.1-3.abc':\n"
                },
            )
            self.assertEqual(
                project_version(directory, "scratch/loose.prisma"),
                ("engines", "7.1.1-3.abc"),
            )


@unittest.skipUnless(
    NODE, "Prisma schemas are read with node, npm and the npm registry"
)
class PrismaSchemaTests(unittest.TestCase):
    def test_schema_stands_for_its_migrations(self):
        files = {
            **PRISMA_FOLDER,
            "apps/api/prisma/migrations/migration_lock.toml": 'provider = "postgresql"\n',
            # An old table, dropped since: its key is not in the schema.
            "apps/api/prisma/migrations/0_init/migration.sql": "CREATE TABLE "
            '"Draft" (id int, "authorId" int REFERENCES "Author" (id));\n',
            # Not Prisma Migrate's: no lock file beside it.
            "db/migrations/0_init/migration.sql": "CREATE TABLE notes "
            "(id int, author int REFERENCES writers (id));\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
            )
        self.assertEqual(
            sorted(
                (
                    r.source_match["qualified_name"].rsplit("::", 1)[-1],
                    r.target_match["qualified_name"].rsplit("::", 1)[-1],
                    r.properties["rule_id"],
                )
                for r in output.relationships
                if r.type == "REFERENCES_TABLE"
            ),
            [
                ("notes", "writers", "data-contracts.sql-foreign-key"),
                ("posts", "author", "data-contracts.prisma-foreign-key"),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["foreign_keys"][
                "prisma_migration_files_superseded"
            ],
            1,
        )

    def test_folders_rejections_and_missing_versions(self):
        from deepthought.prisma_schema import read_schemas

        with tempfile.TemporaryDirectory() as directory:
            files = {k: v for k, v in PRISMA_FOLDER.items() if k.endswith(".prisma")}
            write(directory, PRISMA_FOLDER)
            read = read_schemas(directory, files)
        self.assertEqual(
            [
                (m.file, m.model, m.table, m.explicit, m.line, m.foreign_keys)
                for m in read.models
            ],
            [
                (
                    "apps/api/prisma/schema/author.prisma",
                    "Author",
                    "Author",
                    False,
                    3,
                    (),
                ),
                (
                    "apps/api/prisma/schema/models/post.prisma",
                    "Post",
                    "posts",
                    True,
                    1,
                    # Post holds the foreign key; @map names its column.
                    (("Author", ("author_id",), ("id",), "Cascade"),),
                ),
            ],
        )
        self.assertEqual(
            sorted(read.unread), ["scratch/loose.prisma", "tools/broken/schema.prisma"]
        )
        self.assertIn("no Prisma version", read.unread["scratch/loose.prisma"])
        self.assertIn(
            "opposite relation field", read.unread["tools/broken/schema.prisma"]
        )
        self.assertEqual(
            sorted(read.engines),
            [
                "apps/api/prisma/schema/author.prisma",
                "apps/api/prisma/schema/main.prisma",
                "apps/api/prisma/schema/models/post.prisma",
            ],
        )

    @unittest.skipUnless(TYPESCRIPT, "Install the typescript grammar")
    def test_unread_schema_makes_the_run_partial(self):
        call = 'import { PrismaClient } from "@prisma/client";\n'
        call += "export const posts = () => new PrismaClient().post.findMany();\n"
        with tempfile.TemporaryDirectory() as directory:
            write(
                directory,
                {
                    "scratch/package.json": '{"name": "scratch"}',
                    "scratch/loose.prisma": "model Post {\n  id Int @id\n}\n",
                    "scratch/app.ts": call,
                    "shop/package.json": '{"dependencies": {"prisma": "6.19.0"}}',
                    "shop/schema.prisma": "model Post {\n  id Int @id\n}\n",
                    "shop/app.ts": call,
                },
            )
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["prisma_models"], 1)
        self.assertEqual(metrics["analysis_status"], "partial")
        self.assertEqual(
            metrics["orm_access_resolution"],
            {
                "prisma_linked": 1,
                "prisma_ambiguous": 0,
                "prisma_schema_unread": 1,
                "typeorm_index_unresolved": 0,
                "typeorm_linked": 0,
                "typeorm_name_rule_replaced": 0,
                "typeorm_not_an_entity": 0,
                "typeorm_wrapper_not_an_entity": 0,
            },
        )
        # The scratch caller is not linked to the shop's Post.
        self.assertEqual(
            sorted(
                r.source_match["qualified_name"].split("::")[1]
                for r in output.relationships
                if r.type == "READS"
            ),
            ["shop/app.ts"],
        )
        self.assertTrue(
            any(
                w.startswith(
                    "prisma_schema_unread: scratch/loose.prisma: no Prisma version"
                )
                for w in output.metadata.warnings
            ),
            output.metadata.warnings,
        )


TYPEORM_WRAPPERS = {
    "package.json": '{"name": "stock", "dependencies": {"typeorm": "0.3.28"}}',
    "src/entity.ts": """import { ChildEntity, Entity, TableInheritance } from "typeorm";

@Entity()
export class Order {}

@Entity()
@TableInheritance({ column: { name: "type" } })
export abstract class StockMovement {}

@ChildEntity()
export class Allocation extends StockMovement {}

@ChildEntity()
export class Sale extends Allocation {}
""",
    "src/order.service.ts": """import { In } from "typeorm";
import { Order, Sale } from "./entity";
import { Connection } from "./connection";

export class OrderService {
    constructor(private connection: Connection) {}

    async find(ctx, ids) {
        return this.connection.getRepository(ctx, Order).find({ where: { id: In(ids) } });
    }

    async save(ctx, order) {
        return this.connection.getRepository(ctx, Order).save(order);
    }

    count() {
        return this.connection.raw.getRepository(Order).count();
    }

    query(ctx) {
        return this.connection.getRepository(ctx, Order).createQueryBuilder("o");
    }

    sales(ctx) {
        return this.connection.getRepository(ctx, Sale).findOneBy({});
    }

    dynamic(ctx, entity) {
        // A parameter names no entity.
        return this.connection.getRepository(ctx, entity).find();
    }

    unknown(ctx) {
        // An imported class that is not an entity.
        return this.connection.getRepository(ctx, Connection).find();
    }
}
""",
    "src/connection.ts": """export class Connection {
    raw: any;
    getRepository(ctx, target?) {
        return this.raw.getRepository(target ?? ctx);
    }
}
""",
}


GO = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_go"
)
GO_SQL = {
    "go.mod": "module example.com/shop\n",
    "store/user.go": """package store

import (
\t"context"
\t"database/sql"
)

type User struct{ ID int }

func (User) TableName() string { return "accounts" }

type Session struct{}

func (*Session) TableName() string { return name() }

type Store struct{ db *sql.DB }

func (s *Store) Find(ctx context.Context, id int) {
\ts.db.QueryRowContext(ctx, "SELECT id FROM accounts WHERE id = $1", id)
}

func (s *Store) Purge(table string) {
\ts.db.Exec(`DELETE FROM sessions` + " WHERE expired")
\ts.db.Exec("TRUNCATE " + table)
\ts.db.Exec("VACUUM")
}
""",
    "other/cache.go": """package other

func Get(c Cache) { c.Get("SELECT * FROM nothing") }
""",
}


SCRIPT_SQL = {
    "result/package.json": "{}",
    "result/server.js": """const { Pool } = require('pg');
const pool = new Pool({ connectionString: 'postgres://postgres@db/postgres' });

function getVotes(client) {
  client.query('SELECT vote, COUNT(id) AS count FROM votes GROUP BY vote', [], () => {});
}

async function record(id, vote) {
  await pool.query({ text: 'INSERT INTO votes (id, vote) VALUES ($1, $2)', values: [id, vote] });
  await pool.query(`UPDATE votes SET vote = $1 WHERE id = ${id}`);
  await pool.query('DELETE FROM ' + 'audit' + ' WHERE id = $1', [id]);
}

function dynamic(table) {
  return pool.query(`SELECT * FROM ${table}`);
}

function notSql(cache) {
  return cache.get('select the cached value');
}
""",
    "other/package.json": "{}",
    "other/map.js": """// No SQL driver: a query method here is not read.
export const run = (db) => db.query('SELECT * FROM votes');
""",
}


@unittest.skipUnless(TYPESCRIPT, "Install the typescript grammar")
class ScriptSqlTests(unittest.TestCase):
    def test_query_calls_in_files_importing_a_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, SCRIPT_SQL)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["javascript"])
            )
        rows = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        rule = "data-contracts.script-sql"
        self.assertEqual(
            rows,
            [
                ("READS", "getVotes", "votes", rule),
                ("WRITES", "record", "audit", rule),
                ("WRITES", "record", "votes", rule),
                ("WRITES", "record", "votes", rule),
            ],
        )
        self.assertEqual(output.metadata.custom_metrics["sql_text"]["not_fixed"], 1)


def go_edges(output):
    return sorted(
        (
            r.type,
            r.source_match["qualified_name"].split("::")[-2],
            r.target_match["qualified_name"].rsplit("::", 1)[1],
            r.properties["rule_id"].split(".", 1)[1],
        )
        for r in output.relationships
        if r.type != "CONTAINS"
    )


@unittest.skipUnless(GO, "Install the go grammar")
class GoSqlTests(unittest.TestCase):
    """The tree-sitter rules, as they read Go files the type checker did
    not check."""

    def test_missing_go_fails_loudly(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("deepthought.go_facts.toolchain", return_value=None),
        ):
            write(directory, GO_SQL)
            with self.assertRaises(GoMissing):
                asyncio.run(DataContractTool(directory).analyze("r", languages=["go"]))

    def test_sql_text_and_gorm_table_names(self):
        def unchecked(root, files, modules):
            return GoFacts(uncovered=dict.fromkeys(files, "not_checked"))

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("deepthought.go_facts.collect", side_effect=unchecked),
        ):
            write(directory, GO_SQL)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["go"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            edges,
            [
                ("MAPS_TO", "User", "accounts", "gorm-table-name"),
                ("READS", "Store.Find", "accounts", "go-sql"),
                ("WRITES", "Store.Purge", "sessions", "go-sql"),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            metrics["gorm_table_names"], {"linked": 1, "type_not_found": 0}
        )
        self.assertEqual(
            metrics["sql_text"],
            {
                "statements": 3,
                "not_fixed": 1,
                "unknown_entity": 0,
                "sqlglot": 2,
                "tokens": 0,
                "tokens_not_parsed": 0,
            },
        )
        self.assertEqual(
            metrics["go_type_facts"]["files_not_checked"], {"not_checked": 2}
        )


GO_TYPED = {
    "go.mod": "module example.com/shop\n\ngo 1.22\n\nrequire gorm.io/gorm v1.25.12\n",
    "store/db.go": """package store

import (
\t"context"
\t"database/sql"
)

// DBTX is the interface sqlc writes: its methods return database/sql types.
type DBTX interface {
\tExecContext(context.Context, string, ...interface{}) (sql.Result, error)
\tQueryRowContext(context.Context, string, ...interface{}) *sql.Row
}

type Queries struct{ db DBTX }

// DB embeds *sql.DB: its Exec is database/sql's.
type DB struct{ *sql.DB }
""",
    "store/queries.go": """package store

import "context"

const getAccount = `-- name: GetAccount :one
SELECT id FROM accounts WHERE id = $1`

func (q *Queries) GetAccount(ctx context.Context, id int) {
\tq.db.QueryRowContext(ctx, getAccount, id)
}

func (q *Queries) Archive(ctx context.Context, table string) {
\tquery := "INSERT INTO archive SELECT * FROM " + table
\tq.db.ExecContext(ctx, query)
}

func (q *Queries) Twice(ctx context.Context, late bool) {
\tquery := "DELETE FROM sessions"
\tif late {
\t\tquery = "DELETE FROM audit"
\t}
\tq.db.ExecContext(ctx, query)
}

func Purge(db *DB) { db.Exec("DELETE FROM tokens") }
""",
    "store/models.go": """package store

const usersTable = "users"

type User struct{ ID int }

func (User) TableName() string { return usersTable }
""",
    "store/built.go": """package store

import "strings"

// Queries built in steps: reassigned, appended to, joined over branches.
func (q *Queries) Steps(where []string, limit bool) {
\tquery := "SELECT count(*) FROM categories"
\tq.db.QueryRowContext(nil, query)
\tquery = "UPDATE feeds SET category_id = 1"
\tq.db.ExecContext(nil, query)
\tjobs := `SELECT id FROM jobs`
\tif len(where) > 0 {
\t\tjobs += " WHERE " + strings.Join(where, " AND ")
\t}
\tjobs += " ORDER BY id"
\tif limit {
\t\tjobs += " LIMIT 10"
\t}
\tq.db.QueryRowContext(nil, jobs)
\tvar loop string
\tfor _, w := range where {
\t\tloop = "DELETE FROM " + w
\t}
\tq.db.ExecContext(nil, loop)
}
""",
    "store/migrations.go": """package store

import "database/sql"

// Calls in func literals of a package variable are read too.
var migrations = []func(tx *sql.Tx) error{
\tfunc(tx *sql.Tx) error {
\t\tquery := "INSERT INTO schema_version VALUES (1)"
\t\t_, err := tx.Exec(query)
\t\treturn err
\t},
}
""",
    "web/search.go": """package web

import "net/url"

// Not SQL: url.Values.Get and a Get of the code's own type.
type Cache struct{}

func (Cache) Get(key string) string { return key }

func Lookup(values url.Values, c Cache) {
\tvalues.Get("select from the list")
\tc.Get("SELECT * FROM cached")
}
""",
}


@unittest.skipUnless(GO and shutil.which("go"), "Install the go grammar and Go")
class GoTypeFactsTests(unittest.TestCase):
    """What the Go type checker (go/types) adds to the tree-sitter rules."""

    def analyze(self, files):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            return asyncio.run(
                DataContractTool(directory).analyze("r", languages=["go"])
            )

    def test_typed_calls_constants_and_table_names(self):
        output = self.analyze(GO_TYPED)
        self.assertEqual(
            go_edges(output),
            [
                ("MAPS_TO", "User", "users", "gorm-table-name-types"),
                ("READS", "Queries.GetAccount", "accounts", "go-sql-types"),
                ("READS", "Queries.Steps", "categories", "go-sql-types"),
                ("READS", "Queries.Steps", "jobs", "go-sql-types"),
                ("WRITES", "Purge", "tokens", "go-sql-types"),
                ("WRITES", "Queries.Archive", "archive", "go-sql-types"),
                ("WRITES", "Queries.Steps", "feeds", "go-sql-types"),
                # Either table, by the path taken.
                ("WRITES", "Queries.Twice", "audit", "go-sql-types"),
                ("WRITES", "Queries.Twice", "sessions", "go-sql-types"),
                ("WRITES", "store/migrations.go", "schema_version", "go-sql-types"),
            ],
        )
        metrics = output.metadata.custom_metrics
        # The loop's query is assigned in the loop: not known.
        self.assertEqual(
            metrics["sql_text"],
            {
                "statements": 8,
                "not_fixed": 0,
                "unknown_entity": 0,
                "sqlglot": 8,
                "tokens": 0,
                "tokens_not_parsed": 0,
            },
        )
        self.assertEqual(metrics["go_type_facts"]["modules"], {"checked": 1})
        self.assertEqual(metrics["go_type_facts"]["files_checked"], 6)

    def test_same_calls_unchecked(self):
        # In files go/types did not check none of these is read: the files
        # that run SQL do not import a SQL package, and the TableName is a
        # constant.
        def unchecked(root, files, modules):
            return GoFacts(uncovered=dict.fromkeys(files, "not_checked"))

        with mock.patch("deepthought.go_facts.collect", side_effect=unchecked):
            output = self.analyze(GO_TYPED)
        self.assertEqual(go_edges(output), [])

    def test_untyped_calls_fall_back_to_tree_sitter(self):
        files = {
            "go.mod": "module example.com/shop\n\ngo 1.22\n",
            "store/store.go": """package store

import (
\t"database/sql"

\t"example.com/missing/dep"
)

func Load(db *sql.DB, other dep.Conn) {
\tdb.Query("SELECT id FROM accounts")
\tother.Query("SELECT id FROM orders")
}
""",
        }
        output = self.analyze(files)
        self.assertEqual(
            go_edges(output),
            [
                ("READS", "Load", "accounts", "go-sql-types"),
                ("READS", "Load", "orders", "go-sql"),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["go_type_facts"]["files_partly_checked"], 1
        )

    def test_files_outside_a_module(self):
        # A GOPATH-era package: checked as a module of its own.
        files = {
            "cmd/load/main.go": """package main

import "database/sql"

func Load(db *sql.DB) {
\tdb.Query("SELECT id FROM accounts")
}
""",
        }
        output = self.analyze(files)
        self.assertEqual(
            go_edges(output), [("READS", "Load", "accounts", "go-sql-types")]
        )
        self.assertFalse(
            output.metadata.custom_metrics["go_type_facts"].get("files_not_checked")
        )

    def test_gopath_packages_record_what_they_resolved(self):
        # The package imports a module served by a local module proxy; the
        # versions and go.sum the check resolved are kept on the facts.
        with tempfile.TemporaryDirectory() as proxy:
            _module_proxy(
                proxy,
                "example.com/dbx",
                "v1.2.0",
                {"dbx.go": 'package dbx\n\nconst Table = "accounts"\n'},
            )
            environment = {
                "GOPROXY": Path(proxy).as_uri(),
                "GOSUMDB": "off",
                "GOFLAGS": "",
            }
            files = {
                "cmd/load/main.go": """package main

import (
\t"database/sql"

\t"example.com/dbx"
)

func Load(db *sql.DB) {
\tdb.Query("SELECT id FROM " + dbx.Table)
}
""",
            }
            with mock.patch.dict(os.environ, environment):
                output = self.analyze(files)
        packages = output.metadata.custom_metrics["go_type_facts"]["gopath_packages"]
        self.assertEqual(list(packages), ["cmd/load"])
        self.assertEqual(packages["cmd/load"]["status"], "gopath_latest")
        self.assertEqual(packages["cmd/load"]["modules"], {"example.com/dbx": "v1.2.0"})
        self.assertRegex(packages["cmd/load"]["go_sum_sha256"], "^[0-9a-f]{64}$")


def _module_proxy(directory, path, version, sources):
    """A GOPROXY=file:// directory serving one module version."""
    import json
    import zipfile

    base = Path(directory) / path / "@v"
    base.mkdir(parents=True)
    (base / "list").write_text(version + "\n", encoding="utf-8")
    (base / f"{version}.info").write_text(
        json.dumps({"Version": version, "Time": "2020-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    mod = f"module {path}\n\ngo 1.16\n"
    (base / f"{version}.mod").write_text(mod, encoding="utf-8")
    with zipfile.ZipFile(base / f"{version}.zip", "w") as archive:
        archive.writestr(f"{path}@{version}/go.mod", mod)
        for name, text in sources.items():
            archive.writestr(f"{path}@{version}/{name}", text)


class GoToolchainTests(unittest.TestCase):
    def test_wanted_toolchain(self):
        from deepthought.go_facts import wanted_toolchain

        for text, wanted in (
            ("module m\n\ngo 1.26.0\n", "go1.26.0"),
            ("module m\n\ngo 1.22\n", "go1.22.0"),
            ("module m\n\ngo 1.20\n", "go1.20"),
            ("module m\n\ngo 1.24\n\ntoolchain go1.26.10\n", "go1.26.10"),
            ("module m\n", None),
        ):
            self.assertEqual(wanted_toolchain(text), wanted)

    def test_module_cache_toolchain_only_when_newer_and_present(self):
        from deepthought.go_facts import module_toolchain

        with tempfile.TemporaryDirectory() as cache:
            info = {
                "GOVERSION": "go1.24.7",
                "GOMODCACHE": cache,
                "GOOS": "linux",
                "GOARCH": "amd64",
            }
            newer = "module m\n\ngo 1.26.10\n"
            self.assertEqual(module_toolchain("go", info, newer), "go")
            path = (
                Path(cache) / "golang.org/toolchain@v0.0.1-go1.26.10.linux-amd64/bin/go"
            )
            path.parent.mkdir(parents=True)
            path.write_text("")
            self.assertEqual(module_toolchain("go", info, newer), str(path))
            older = "module m\n\ngo 1.22\n"
            self.assertEqual(module_toolchain("go", info, older), "go")


SHARED_COMPOSE_DATABASE = {
    "docker-compose.yml": """\
services:
  pg:
    image: postgres:16-alpine
  api:
    build: .
    environment:
      DATABASE_URL: postgres://app:secret@pg:5432/shop
  checker:
    build: ./checker
    environment:
      - DB_URL=postgresql://pg/shop
  legacy:
    build: ./legacy
    environment:
      - DB_URL=postgres://pg/archive
  web:
    build: ./web
    environment:
      - API_URL=http://api:3000
  cache:
    build: ./cache
    environment:
      - DB_URL=${DB_URL}
""",
    "package.json": "{}",
    "db/package.json": '{"name": "db"}',
    "db/schema.sql": "CREATE TABLE monitor (id int);\n",
    "checker/go.mod": "module example.com/checker\n",
    "legacy/go.mod": "module example.com/legacy\n",
    "web/go.mod": "module example.com/web\n",
    "cache/go.mod": "module example.com/cache\n",
}
for _service in ("checker", "legacy", "web", "cache"):
    SHARED_COMPOSE_DATABASE[f"{_service}/store.go"] = f"""package store

import "database/sql"

func {_service.capitalize()}(db *sql.DB) {{
\tdb.Query("SELECT id FROM monitor")
}}
"""


class ComposeDatabaseTests(unittest.TestCase):
    @needs_deploy_tools
    def test_services_naming_one_compose_database_share_its_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, SHARED_COMPOSE_DATABASE)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["go"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        # checker names the api's database: one monitor table. legacy names
        # another database of the same server; web names no database and
        # cache's URL is interpolated, so both keep their package.
        self.assertEqual(
            edges,
            [
                (
                    "DECLARES",
                    "db/schema.sql",
                    "r::compose:.#pg/shop::db-table::monitor",
                ),
                ("READS", "cache/store.go", "r::cache/go.mod::db-table::monitor"),
                (
                    "READS",
                    "checker/store.go",
                    "r::compose:.#pg/shop::db-table::monitor",
                ),
                (
                    "READS",
                    "legacy/store.go",
                    "r::compose:.#pg/archive::db-table::monitor",
                ),
                ("READS", "web/store.go", "r::web/go.mod::db-table::monitor"),
            ],
        )


KNEX = {
    "package.json": '{"dependencies": {"knex": "3.1.0"}}',
    "migrations/001_init.js": """exports.up = async function (knex) {
  await knex.schema.createTable('users', (table) => { table.increments(); });
  await knex.schema.withSchema('app').createTable('Teams', () => {});
  await knex.schema.table('users', (table) => { table.string('bio'); });
  await knex.batchInsert('audit', []);
};
""",
    "src/store.ts": """import type { Knex } from 'knex';

export class Store {
  constructor(private readonly db: Knex) {}

  async find(id: string) {
    return this.db<Row>('users as u')
      .leftJoin('app.teams as t', 'u.team_id', 't.id')
      .where('u.id', id)
      .first();
  }

  async purge(tx: Knex | Knex.Transaction, opaque: unknown) {
    const trx = opaque as Knex.Transaction;
    await trx.insert({ id: 1 }).into('archive');
    const query = tx('sessions').where('expired', true);
    const rows = await query.clone().select('id');
    await query.del();
    return rows;
  }
}
""",
    "src/report.js": """async function report(db) {
  await db.knex.transaction(async (t) => {
    await t('teams').update({ active: false });
  });
  return db.knex
    .with('recent', db.knex('logins').select('*'))
    .select('*')
    .from('recent');
}
function other(x) {
  return x.select('id').from('not_knex');
}
""",
}


class KnexTests(unittest.TestCase):
    maxDiff = None

    def test_queries_and_created_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, KNEX)
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["javascript", "typescript"]
                )
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[1],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["line"],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES", "DECLARES")
        )
        self.assertEqual(
            edges,
            [
                ("DECLARES", "migrations/001_init.js", "teams", 3, "knex-create-table"),
                ("DECLARES", "migrations/001_init.js", "users", 2, "knex-create-table"),
                ("READS", "src/report.js", "logins", 6, "knex-query"),
                ("READS", "src/store.ts", "sessions", 16, "knex-query"),
                ("READS", "src/store.ts", "teams", 8, "knex-query"),
                ("READS", "src/store.ts", "users", 7, "knex-query"),
                ("WRITES", "migrations/001_init.js", "audit", 5, "knex-query"),
                ("WRITES", "src/report.js", "teams", 3, "knex-query"),
                ("WRITES", "src/store.ts", "archive", 15, "knex-query"),
                ("WRITES", "src/store.ts", "sessions", 16, "knex-query"),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["knex"], {"queries": 7, "created": 2}
        )

    def test_constant_tables_and_knex_type_aliases(self):
        files = {
            "package.json": '{"dependencies": {"knex": "3"}}',
            "src/tables.ts": (
                "export const TASKS = 'tasks';\n"
                "export const TWICE = 'a';\n"
                "export const TWICE = 'b';\n"
                "export let LOOSE = 'loose';\n"
            ),
            "src/repo.ts": (
                "import { type Knex } from 'knex';\n"
                "import { TASKS, TWICE, LOOSE } from './tables';\n"
                "const RUNS = 'runs';\n"
                "type Connection = Knex | Knex.Transaction;\n"
                "export async function list(db: Connection) {\n"
                "  await db(TASKS).select();\n"  # 6: imported constant
                "  await db(RUNS).insert({});\n"  # 7: the file's constant
                "  await db(TWICE).select();\n"  # 8: declared twice
                "  await db(LOOSE).select();\n"  # 9: not a const
                "}\n"
                "export async function shadow(db: Knex, RUNS: string) {\n"
                "  await db(RUNS).select();\n"  # 12: a parameter, not the const
                "}\n"
                "export async function sub(query: Knex.QueryBuilder) {\n"
                "  await query.from('steps');\n"  # 15: a typed query builder
                "}\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        edges = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["line"],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        self.assertEqual(
            edges,
            [
                ("READS", "steps", 15),
                ("READS", "tasks", 6),
                ("WRITES", "runs", 7),
            ],
        )

    def test_without_knex_nothing_is_read(self):
        files = {
            "package.json": "{}",
            "src/a.js": "async function f(knex) { await knex('users').del(); }\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["javascript"])
            )
        self.assertEqual(
            [r for r in output.relationships if r.type in ("READS", "WRITES")], []
        )


RAILS = {
    "db/schema.rb": """\
ActiveRecord::Schema[7.1].define(version: 2024_01_01_000000) do
  create_table "people", force: :cascade do |t|
    t.string "name"
  end
  create_table "legacy_orders" do |t|
  end
  create_table "admin_audit_entries" do |t|
  end
end
""",
    "app/models/application_record.rb": """\
class ApplicationRecord < ActiveRecord::Base
  primary_abstract_class
end
""",
    "app/models/person.rb": """\
class Person < ApplicationRecord
  def self.adults
    where("age >= 18")
  end
end
""",
    "app/models/employee.rb": "class Employee < Person\nend\n",
    "app/models/git_hub_user.rb": "class GitHubUser < ApplicationRecord\nend\n",
    "config/initializers/inflections.rb": """\
ActiveSupport::Inflector.inflections(:en) do |inflect|
  inflect.acronym "GitHub"
end
""",
    "app/models/order.rb": """\
class Order < ApplicationRecord
  self.table_name = "legacy_orders"

  class LineItem < ApplicationRecord
  end
end
""",
    "app/models/admin.rb": """\
module Admin
  def self.table_name_prefix
    "admin_"
  end
end
""",
    "app/models/admin/audit_entry.rb": """\
module Admin
  class AuditEntry < ApplicationRecord
  end
end
""",
    "app/models/dynamic.rb": """\
class Dynamic < ApplicationRecord
  self.table_name = ENV["TABLE"]
end
""",
    "app/services/payroll.rb": """\
class Payroll
  def run
    Employee.where(active: true).pluck(:id)
    Order.create!(total: 1)
    Order::LineItem.delete_all
    Admin::AuditEntry.insert_all([])
    Payroll.find(1)
  end
end

# Module.nesting is [Admin::Report]: AuditEntry is not Admin::AuditEntry.
class Admin::Report
  def run
    AuditEntry.count
  end
end
""",
}


class SourceLimitTests(unittest.TestCase):
    def test_file_limit_is_reported_and_can_be_raised(self):
        files = {
            f"app/models/m{i}.rb": f"class M{i} < ActiveRecord::Base\nend\n"
            for i in range(3)
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            capped = asyncio.run(
                DataContractTool(directory, max_files=2).analyze(
                    "r", languages=["ruby"]
                )
            )
            full = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["ruby"])
            )
        metrics = capped.metadata.custom_metrics
        self.assertEqual(metrics["analysis_status"], "partial")
        self.assertEqual(metrics["source_limits"], ["file_count_limit"])
        self.assertEqual(metrics["active_record_models"], 2)
        self.assertTrue(
            any("max_files=2" in w for w in capped.metadata.warnings),
            capped.metadata.warnings,
        )
        self.assertEqual(full.metadata.custom_metrics["source_limits"], [])
        self.assertEqual(full.metadata.custom_metrics["active_record_models"], 3)


class RailsTests(unittest.TestCase):
    def test_inflector_matches_active_support(self):
        from deepthought.data_contracts import (
            rails_pluralize,
            rails_singularize,
            rails_table_name,
        )

        # Cases from Rails' inflector_test_cases.rb (activesupport 8.0.2).
        for singular, plural in (
            ("search", "searches"),
            ("person", "people"),
            ("half", "halves"),
            ("matrix", "matrices"),
            ("analysis", "analyses"),
            ("database", "databases"),
            ("status", "statuses"),
            ("equipment", "equipment"),
            ("bus", "buses"),
            ("ox", "oxen"),
        ):
            self.assertEqual(rails_pluralize(singular), plural)
            self.assertEqual(rails_singularize(plural), singular)
        self.assertEqual(rails_table_name("PrimarySpokesman"), "primary_spokesmen")
        self.assertEqual(rails_table_name("Admin::HTTPLog"), "http_logs")

    def test_models_tables_and_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, RAILS)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["ruby"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            edges,
            [
                (
                    "DECLARES",
                    "db/schema.rb",
                    "admin_audit_entries",
                    "rails-schema-table",
                ),
                (
                    "DECLARES",
                    "db/schema.rb",
                    "legacy_orders",
                    "rails-schema-table",
                ),
                ("DECLARES", "db/schema.rb", "people", "rails-schema-table"),
                (
                    "MAPS_TO",
                    "Admin.AuditEntry",
                    "admin_audit_entries",
                    "active-record-default-table-name",
                ),
                ("MAPS_TO", "Employee", "people", "active-record-default-table-name"),
                (
                    "MAPS_TO",
                    "GitHubUser",
                    "github_users",
                    "active-record-default-table-name",
                ),
                ("MAPS_TO", "Order", "legacy_orders", "active-record-table-name"),
                (
                    "MAPS_TO",
                    "Order.LineItem",
                    "legacy_order_line_items",
                    "active-record-default-table-name",
                ),
                ("MAPS_TO", "Person", "people", "active-record-default-table-name"),
                ("READS", "Payroll.run", "people", "active-record-call"),
                ("WRITES", "Payroll.run", "admin_audit_entries", "active-record-call"),
                (
                    "WRITES",
                    "Payroll.run",
                    "legacy_order_line_items",
                    "active-record-call",
                ),
                ("WRITES", "Payroll.run", "legacy_orders", "active-record-call"),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["active_record_models"], 6)
        self.assertEqual(metrics["active_record_unresolved"], 1)
        self.assertEqual(
            metrics["active_record_access_resolution"],
            {"linked": 4, "not_a_model": 2},
        )

    def test_application_naming_config_leaves_defaults_out(self):
        files = {
            **RAILS,
            "config/application.rb": (
                "module App\n  class Application < Rails::Application\n"
                '    config.active_record.table_name_prefix = "app_"\n  end\nend\n'
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["ruby"])
            )
        mapped = sorted(
            r.target_match["qualified_name"].rsplit("::", 1)[1]
            for r in output.relationships
            if r.type == "MAPS_TO"
        )
        self.assertEqual(mapped, ["legacy_orders"])

    def test_class_prefixes_vendored_code_and_call_shorthand(self):
        files = {
            "app/models/application_record.rb": (
                "class ApplicationRecord < ActiveRecord::Base\n"
                "  self.abstract_class = true\nend\n"
            ),
            "app/models/gdpr_delete_request.rb": (
                "class GDPRDeleteRequest < ApplicationRecord\n"
                '  def self.table_name_prefix\n    "users_"\n  end\nend\n'
            ),
            "app/models/users/setting.rb": (
                "module Users\n  class Setting < ApplicationRecord\n"
                '    self.table_name_prefix = "users_"\n  end\nend\n'
            ),
            "app/models/users/notification_setting.rb": (
                "module Users\n  class NotificationSetting < Users::Setting\n"
                "  end\nend\n"
            ),
            "app/models/users/base.rb": (
                "module Users\n  class Base < ApplicationRecord\n"
                "    self.abstract_class = true\n"
                '    self.table_name_prefix = "users_"\n  end\nend\n'
            ),
            "app/models/users/profile.rb": (
                "module Users\n  class Profile < Base\n  end\nend\n"
            ),
            "app/models/follow.rb": "class Follow < ApplicationRecord\nend\n",
            "vendor/cache/acts_as_follower/templates/model.rb": (
                "class Follow < ActiveRecord::Base\nend\n"
            ),
            "app/services/list.rb": (
                "class List\n  def call\n    step.(1)\n    Follow.where(a: 1)\n"
                "  end\nend\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["ruby"])
            )
        mapped = sorted(
            (
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
            )
            for r in output.relationships
            if r.type == "MAPS_TO"
        )
        self.assertEqual(
            mapped,
            [
                ("Follow", "follows"),
                ("GDPRDeleteRequest", "users_gdpr_delete_requests"),
                ("Users.NotificationSetting", "users_settings"),
                ("Users.Profile", "users_profiles"),
                ("Users.Setting", "users_settings"),
            ],
        )
        reads = [r for r in output.relationships if r.type == "READS"]
        self.assertEqual(len(reads), 1)


GORM = {
    "go.mod": "module example.com/shop\n\ngo 1.22\n\nrequire gorm.io/gorm v1.25.12\n",
    "models/models.go": """\
package models

import "gorm.io/gorm"

type UserModel struct {
	gorm.Model
	Name string
}

type Address struct {
	ID uint
}

type Legacy struct {
	ID uint
}

func (Legacy) TableName() string { return "legacy_rows" }

type Plain struct {
	ID uint
}

func Migrate(db *gorm.DB) {
	db.AutoMigrate(&Address{})
}
""",
    "store/store.go": """\
package store

import "example.com/shop/models"

func Load(db DB, id uint) (models.UserModel, error) {
	var user models.UserModel
	err := db.First(&user, id).Error
	return user, err
}

func Rename(db DB, user *models.UserModel) {
	db.Model(user).Update("name", "x")
	db.Model(user).Association("Addresses").Find(&[]models.Address{})
}

func Save(db DB, value interface{}, rows []models.Legacy) {
	db.Save(value)
	db.Create(&models.Address{})
	db.Find(&rows)
	db.Delete(&models.Plain{})
}
""",
}


class GormTests(unittest.TestCase):
    @needs_go
    @unittest.skipUnless(GORM_RELEASES, "needs the Go module proxy")
    def test_default_names_come_from_gorm(self):
        from deepthought.gorm_naming import table_names

        # GORM's NamingStrategy at the version the module selects; these are
        # gorm.io/gorm 1.25.12's answers.
        with tempfile.TemporaryDirectory() as directory:
            write(
                directory,
                {
                    "svc/go.mod": "module example.com/svc\n\ngo 1.22\n\n"
                    "require gorm.io/gorm v1.25.12\n",
                },
            )
            names, reason = table_names(
                directory,
                "svc",
                ["UserModel", "HTTPSServer", "Human", "APIKey", "Person", "Leaf"],
            )
            self.assertIsNone(reason)
            self.assertEqual(
                names,
                {
                    "UserModel": "user_models",
                    "HTTPSServer": "http_s_servers",
                    "Human": "humen",
                    "APIKey": "api_keys",
                    "Person": "people",
                    "Leaf": "leafs",
                },
            )
            # A module whose go.mod selects no GORM gets no names, and why.
            write(directory, {"bare/go.mod": "module example.com/bare\n\ngo 1.22\n"})
            names, reason = table_names(directory, "bare", ["User"])
            self.assertEqual(names, {})
            self.assertIn("the module graph selects no gorm.io/gorm", reason)

    @needs_go
    @unittest.skipUnless(GORM_RELEASES, "needs the Go module proxy")
    def test_models_and_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, GORM)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["go"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            edges,
            [
                ("MAPS_TO", "Address", "addresses", "gorm-default-table-name"),
                # The go.mod requires GORM, so go/types reads TableName.
                ("MAPS_TO", "Legacy", "legacy_rows", "gorm-table-name-types"),
                ("MAPS_TO", "UserModel", "user_models", "gorm-default-table-name"),
                ("READS", "Load", "user_models", "gorm-call"),
                # Association(...).Find reads the associated table.
                ("READS", "Rename", "addresses", "gorm-call"),
                ("READS", "Save", "legacy_rows", "gorm-call"),
                ("WRITES", "Rename", "user_models", "gorm-call"),
                ("WRITES", "Save", "addresses", "gorm-call"),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["gorm_default_table_names"], 2)
        self.assertEqual(
            metrics["gorm_access_resolution"], {"linked": 5, "not_a_model": 1}
        )

    @needs_go
    def test_naming_strategy_leaves_defaults_out(self):
        files = {
            **GORM,
            "db/open.go": """\
package db

import (
	"gorm.io/gorm"
	"gorm.io/gorm/schema"
)

var Config = gorm.Config{NamingStrategy: schema.NamingStrategy{SingularTable: true}}
""",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["go"])
            )
        mapped = sorted(
            r.target_match["qualified_name"].rsplit("::", 1)[1]
            for r in output.relationships
            if r.type == "MAPS_TO"
        )
        self.assertEqual(mapped, ["legacy_rows"])


DRIZZLE = {
    "package.json": '{"name": "shop"}',
    "src/db/schema/users.ts": """\
import { pgTable, pgTableCreator, serial, text } from "drizzle-orm/pg-core";

export const users = pgTable("users", { id: serial("id") });
const createTable = pgTableCreator((name) => `shop_${name}`);
export const orders = createTable("orders", { id: serial("id") });
const dynamic = pgTableCreator((name) => name.toUpperCase());
export const audit = dynamic("audit", { id: serial("id") });
""",
    "src/db/schema/index.ts": 'export * from "./users";\n',
    "src/db/index.ts": 'export * as schema from "./schema";\n',
    "src/orders.ts": """\
import { eq } from "drizzle-orm";
import { db } from "./client";
import { orders, users as people } from "./db/schema";
import { schema } from "./db";

export async function place(id: number) {
  await db.insert(orders).values({ id });
  const rows = await db
    .select()
    .from(people)
    .innerJoin(schema.orders, eq(people.id, id));
  await db.query.users.findMany();
  await db.delete(audit);
  return Array.from(rows);
}
""",
}


DRIZZLE_WORKSPACE = {
    "packages/db/package.json": '{"name": "@acme/db", "exports": {".": "./src/index.ts", '
    '"./schema": {"types": "./src/schema/index.ts", "import": "./src/schema/index.ts"}}}',
    "packages/db/src/index.ts": "export const version = 1;\n",
    "packages/db/src/schema/index.ts": "export * from './posts';\n",
    "packages/db/src/schema/posts.ts": """\
import { pgTable, text } from 'drizzle-orm/pg-core';

export const posts = pgTable('post', { id: text('id') });
""",
    "apps/core/package.json": '{"name": "core", "dependencies": {"@acme/db": "workspace:*"}}',
    "apps/core/src/database/schema.ts": "export * from '@acme/db/schema';\n",
    "apps/core/src/posts.service.ts": """\
import { posts } from './database/schema';

export const list = (db) => db.select().from(posts);
export const add = (db) => db.insert(posts).values({});
""",
    # A client-side collection named like a table is not the table.
    "apps/admin/package.json": '{"name": "admin"}',
    "apps/admin/src/post.ts": "export const posts = createCollection('posts');\n",
    "apps/admin/src/post.mutations.ts": """\
import { posts } from './post';

export const remove = (tx, id) => tx.delete(posts, id);
""",
}


class DrizzleTests(unittest.TestCase):
    def test_imports_through_a_workspace_package(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DRIZZLE_WORKSPACE)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        rows = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.source_match["qualified_name"].split("::")[1],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        service = "apps/core/src/posts.service.ts"
        self.assertEqual(
            rows, [("READS", "post", service), ("WRITES", "post", service)]
        )
        self.assertEqual(
            output.metadata.custom_metrics["drizzle_access_resolution"],
            {"linked": 2, "not_a_table": 1},
        )

    def test_tables_and_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DRIZZLE)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            edges,
            [
                ("DECLARES", "src/db/schema/users.ts", "shop_orders", "drizzle-table"),
                ("DECLARES", "src/db/schema/users.ts", "users", "drizzle-table"),
                ("READS", "place", "shop_orders", "drizzle-query"),
                ("READS", "place", "users", "drizzle-query"),
                ("READS", "place", "users", "drizzle-query"),
                ("WRITES", "place", "shop_orders", "drizzle-query"),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["drizzle_tables"], {"tables": 2, "not_fixed": 1})
        self.assertEqual(
            metrics["drizzle_access_resolution"], {"linked": 4, "not_a_table": 0}
        )


MYBATIS = {
    "pom.xml": "<project><artifactId>shop</artifactId></project>",
    "src/main/java/com/shop/OrderMapper.java": """\
package com.shop;

import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Select;

public interface OrderMapper {
    Order find(long id);
    void place(Order order);
    java.util.List<Order> search(String table);

    @Select({"SELECT o.* FROM orders o", "JOIN customers c ON c.id = o.customer_id"})
    java.util.List<Order> withCustomers();

    @Delete("<script>DELETE FROM orders <where><if test='id != null'>id = #{id}</if></where></script>")
    void purge(Long id);
}
""",
    "src/main/resources/com/shop/OrderMapper.xml": """\
<?xml version="1.0" encoding="UTF-8" ?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
  "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.shop.OrderMapper">
  <sql id="columns">id, total</sql>
  <select id="find" resultType="Order">
    SELECT <include refid="columns"/> FROM orders
    <where>
      <if test="id != null">id = #{id}</if>
      <if test="vip">AND id IN (SELECT order_id FROM vip_orders)</if>
    </where>
  </select>
  <insert id="place">
    <selectKey keyProperty="id" resultType="long" order="BEFORE">
      SELECT nextval FROM order_seq
    </selectKey>
    INSERT INTO orders (id, total) VALUES (#{id}, #{total})
  </insert>
  <select id="search">SELECT * FROM ${table}</select>
  <select id="nested">SELECT * FROM order_lines</select>
</mapper>
""",
}


class MyBatisTests(unittest.TestCase):
    def test_mapper_xml_and_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, MYBATIS)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["java"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            edges,
            [
                ("READS", "OrderMapper.find", "orders", "mybatis-mapper-xml"),
                ("READS", "OrderMapper.find", "vip_orders", "mybatis-mapper-xml"),
                (
                    "READS",
                    "OrderMapper.withCustomers",
                    "customers",
                    "mybatis-annotation",
                ),
                ("READS", "OrderMapper.withCustomers", "orders", "mybatis-annotation"),
                # A statement no mapper method names belongs to its file.
                (
                    "READS",
                    "src/main/resources/com/shop/OrderMapper.xml",
                    "order_lines",
                    "mybatis-mapper-xml",
                ),
                ("WRITES", "OrderMapper.place", "orders", "mybatis-mapper-xml"),
                ("WRITES", "OrderMapper.purge", "orders", "mybatis-annotation"),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(
            metrics["mybatis_mapper_statements"],
            {"statements": 4, "not_fixed": 1, "method_found": 3},
        )


LARAVEL = {
    "database/migrations/2024_01_01_000000_create_tables.php": """\
<?php
use Illuminate\\Support\\Facades\\Schema;

return new class {
    public function up(): void
    {
        Schema::create('user_profiles', function ($table) {});
        Schema::create('legacy_songs_new', function ($table) {});
        Schema::rename('legacy_songs_new', 'legacy_songs');
    }
};
""",
    "app/Models/Base.php": """\
<?php
namespace App\\Models;

use Illuminate\\Database\\Eloquent\\Model;

abstract class Base extends Model
{
}
""",
    "app/Models/UserProfile.php": """\
<?php
namespace App\\Models;

class UserProfile extends Base
{
}
""",
    "app/Models/Song.php": """\
<?php
namespace App\\Models;

use Illuminate\\Database\\Eloquent\\Model as Eloquent;

class Song extends Eloquent
{
    protected $table = 'legacy_songs';
}
""",
    "app/Models/Single.php": """\
<?php
namespace App\\Models;

class Single extends Song
{
}
""",
    "app/Models/Membership.php": """\
<?php
namespace App\\Models;

use Illuminate\\Database\\Eloquent\\Relations\\Pivot;

class Membership extends Pivot
{
}
""",
    "app/Services/Library.php": """\
<?php
namespace App\\Services;

use App\\Models\\{Song, UserProfile as Profile};
use Illuminate\\Support\\Arr;
use Illuminate\\Support\\Facades\\DB;

class Library
{
    public function prune(): void
    {
        Song::where('plays', 0)->delete();
        Profile::firstOrCreate(['id' => 1]);
        DB::table('audit as a')->insert([]);
        Arr::where([], fn () => true);
    }
}
""",
}


SEQUELIZE = {
    "package.json": '{"name": "shop"}',
    "tsconfig.json": """{
  // Comments and trailing commas, as tsconfig allows.
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@server/*": ["./server/*"], },
  },
}
""",
    "migrations/20240101-create.js": """\
module.exports = {
  async up(queryInterface, Sequelize) {
    await queryInterface.createTable("Users", {});
    await queryInterface.createTable({ tableName: "line_items" }, {});
    await queryInterface.renameTable("Tags", "labels");
    await somethingElse.createTable("not_a_table", {});
  },
};
""",
    "server/db.js": """\
const { Sequelize } = require("sequelize");
const sequelize = new Sequelize("sqlite::memory:", {
  define: { underscored: true },
});
module.exports = sequelize;
""",
    "server/models/user.js": """\
const { Model, DataTypes } = require("sequelize");
module.exports = (sequelize) => {
  class User extends Model {}
  User.init({ name: DataTypes.STRING }, { sequelize, underscored: false });
  return User;
};
""",
    "server/models/tag.js": """\
module.exports = (sequelize, DataTypes) => {
  const Tag = sequelize.define("tag", {}, { tableName: "labels" });
  const Note = sequelize.define("Note", {}, { tableName: dynamicName });
  return Tag;
};
""",
    "server/models/base/IdModel.ts": """\
import { Model as SequelizeModel } from "sequelize-typescript";
class IdModel extends SequelizeModel {}
export default IdModel;
""",
    "app/models/base/IdModel.ts": """\
export default class IdModel {}
""",
    "server/models/LineItem.ts": """\
import { Table } from "sequelize-typescript";
import IdModel from "@server/models/base/IdModel";

@Table({ modelName: "LineItem" })
class LineItem extends IdModel {}
export default LineItem;
""",
    "server/routes/orders.js": """\
const db = require("../models");
const { User } = require("../models");
const Widget = require("./widget");

async function checkout(sequelize) {
  const user = await User.findByPk(1);
  await db.LineItem.bulkCreate([]);
  await sequelize.models.tag.findOrCreate({ where: {} });
  await Widget.create({});
  await user.save();
}
module.exports = { checkout };
""",
    # Test sources are skipped by every script reader.
    "server/routes/orders.test.js": """\
const { User } = require("../models");

it("finds", async () => { await User.findAll(); });
""",
    "server/__tests__/factories.ts": """\
import { User } from "../models";

export const buildUser = () => User.create({});
""",
}


class SequelizeTests(unittest.TestCase):
    def test_default_names_match_sequelize(self):
        from deepthought.data_contracts import (
            inflection_pluralize,
            inflection_underscore,
            sequelize_table_name,
        )

        for word, plural in (
            ("person", "people"),
            ("Category", "Categories"),
            ("status", "statuses"),
            ("analysis", "analyses"),
            ("news", "news"),
            ("men", "men"),
            ("datum", "data"),
            ("bus", "buses"),
        ):
            self.assertEqual(inflection_pluralize(word), plural)
        self.assertEqual(
            inflection_underscore("MessageProperties"), "message_properties"
        )
        self.assertEqual(sequelize_table_name("UserProfile"), "UserProfiles")
        self.assertEqual(
            sequelize_table_name("UserProfile", underscored=True), "user_profiles"
        )
        self.assertEqual(
            sequelize_table_name("UserProfile", freeze=True), "UserProfile"
        )

    def test_migrations_models_and_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, SEQUELIZE)
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["javascript", "typescript"]
                )
            )
        rows = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
                r.properties.get("model"),
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            rows,
            [
                ("DECLARES", "labels", "sequelize-migration-table", None),
                ("DECLARES", "line_items", "sequelize-migration-table", None),
                ("DECLARES", "users", "sequelize-migration-table", None),
                ("MAPS_TO", "labels", "sequelize-table-name", "tag"),
                ("MAPS_TO", "line_items", "sequelize-default-table-name", "LineItem"),
                ("MAPS_TO", "users", "sequelize-default-table-name", "User"),
                ("READS", "labels", "sequelize-call", "tag"),
                ("READS", "users", "sequelize-call", "User"),
                ("WRITES", "labels", "sequelize-call", "tag"),
                ("WRITES", "line_items", "sequelize-call", "LineItem"),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["sequelize_models"], 3)
        self.assertEqual(metrics["sequelize_unresolved"], 1)
        self.assertEqual(
            metrics["sequelize_access_resolution"], {"linked": 3, "not_a_model": 1}
        )


PHP_SQL = {
    "app/Dao/EntryDao.php": """\
<?php
namespace App\\Dao;

use Illuminate\\Support\\Facades\\DB;

class EntryDao
{
    public function save($pdo, $id, $checked)
    {
        $stm = $pdo->prepare('UPDATE `_entry` SET read = 1 WHERE id = ?');
        $sql = <<<'SQL'
            SELECT * FROM `_feed`
            SQL;
        $sql .= ' WHERE id = :id';
        $pdo->query($sql);
        if ($checked) {
            $sql = "INSERT INTO entry_tags(id) VALUES ({$id})";
        } else {
            $sql = 'DELETE FROM entry_tags WHERE id = ' . $id;
        }
        $pdo->exec($sql);
        $sql = 'SELECT * FROM categories';
        $pdo->query($sql);
        mysqli_query($link, sprintf('SELECT * FROM %s WHERE a = 1', $table));
        pg_query("SELECT * FROM accounts");
        DB::select('select * from users where active = ?', [1]);
        DB::connection('audit')->statement('DELETE FROM audit_log');
        $xpath->query('//item');
        $request->query('page');
    }
}
""",
}


class PhpSqlTests(unittest.TestCase):
    def test_sql_text_in_pdo_mysqli_pg_and_db_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, PHP_SQL)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["php"])
            )
        rows = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["line"],
            )
            for r in output.relationships
            if r.properties.get("rule_id") == "data-contracts.php-sql"
        )
        self.assertEqual(
            rows,
            [
                ("READS", "_feed", 15),
                ("READS", "accounts", 25),
                ("READS", "categories", 23),
                ("READS", "users", 26),
                ("WRITES", "_entry", 10),
                ("WRITES", "audit_log", 27),
                # Either branch's text may run: both write entry_tags.
                ("WRITES", "entry_tags", 21),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["sql_text"],
            {
                "statements": 8,
                "not_fixed": 1,
                "unknown_entity": 0,
                "sqlglot": 6,
                "tokens": 1,
                "tokens_not_parsed": 0,
            },
        )


class LaravelTests(unittest.TestCase):
    def test_default_names_match_laravel(self):
        from deepthought.data_contracts import laravel_table_name

        for name, table in (
            ("UserProfile", "user_profiles"),
            ("Person", "people"),
            ("SongAnalysis", "song_analyses"),
            ("Criterion", "criteria"),
            ("Media", "media"),
            ("HTTPLog", "h_t_t_p_logs"),
            ("Related", "related"),
            ("App\\Models\\Category", "categories"),
        ):
            self.assertEqual(laravel_table_name(name), table)

    def test_migrations_models_and_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, LARAVEL)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["php"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[-2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS" and r.type != "DECLARES"
        )
        declared = sorted(
            r.target_match["qualified_name"].rsplit("::", 1)[1]
            for r in output.relationships
            if r.type == "DECLARES"
        )
        self.assertEqual(
            declared, ["legacy_songs", "legacy_songs_new", "user_profiles"]
        )
        self.assertEqual(
            edges,
            [
                (
                    "MAPS_TO",
                    "App.Models.Single",
                    "legacy_songs",
                    "eloquent-table-name",
                ),
                ("MAPS_TO", "App.Models.Song", "legacy_songs", "eloquent-table-name"),
                (
                    "MAPS_TO",
                    "App.Models.UserProfile",
                    "user_profiles",
                    "eloquent-default-table-name",
                ),
                (
                    "READS",
                    "App.Services.Library.prune",
                    "legacy_songs",
                    "eloquent-call",
                ),
                (
                    "READS",
                    "App.Services.Library.prune",
                    "user_profiles",
                    "eloquent-call",
                ),
                ("WRITES", "App.Services.Library.prune", "audit", "laravel-db-table"),
                (
                    "WRITES",
                    "App.Services.Library.prune",
                    "legacy_songs",
                    "eloquent-call",
                ),
                (
                    "WRITES",
                    "App.Services.Library.prune",
                    "user_profiles",
                    "eloquent-call",
                ),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["eloquent_models"], 3)
        self.assertEqual(metrics["eloquent_unresolved"], 1)
        self.assertEqual(
            metrics["eloquent_access_resolution"],
            {"linked": 3, "not_a_model": 1, "not_fixed": 0},
        )


class GraphCase(unittest.IsolatedAsyncioTestCase):
    """Writes a workspace's tree-sitter, components and data-contracts
    outputs to an isolated repository of the provisioned Neo4j."""

    files = WORKSPACE

    async def asyncSetUp(self):
        from deepthought.graph_queries import GraphQueryService
        from deepthought.storage.graph import (
            Neo4jConnectionManager,
            WriteCoordinator,
            setup_schema,
        )

        self.manager = Neo4jConnectionManager(
            os.environ["DEEPTHOUGHT_NEO4J_URI"],
            os.environ.get("DEEPTHOUGHT_NEO4J_USER", "neo4j"),
            os.environ["DEEPTHOUGHT_NEO4J_PASSWORD"],
            database=os.environ.get("DEEPTHOUGHT_NEO4J_DATABASE", "neo4j"),
        )
        await self.manager.connect()
        self.addAsyncCleanup(self.manager.close)
        await setup_schema(self.manager.driver, database=self.manager.database)
        self.repo = "data-" + uuid4().hex
        self.addAsyncCleanup(self.remove)
        self.service = GraphQueryService(
            self.manager.driver, database=self.manager.database
        )
        with tempfile.TemporaryDirectory() as directory:
            write(directory, self.files)
            self.outputs = [
                await TreeSitterTool(directory).analyze(self.repo, languages={"java"}),
                await ComponentsTool(directory).analyze(self.repo),
                await DataContractTool(directory).analyze(self.repo),
            ]
        self.snapshot = "rev-" + uuid4().hex
        written = await WriteCoordinator(
            self.manager.driver, database=self.manager.database
        ).write_outputs(self.outputs, self.repo, snapshot_id=self.snapshot)
        self.assertTrue(written.success, written.errors)

    async def remove(self):
        async with self.manager.driver.session(
            database=self.manager.database
        ) as session:
            await (
                await session.run(
                    "MATCH (n {repo_url:$repo}) DETACH DELETE n", repo=self.repo
                )
            ).consume()


@unittest.skipUnless(
    JAVA and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the java grammar",
)
class ImpactTests(GraphCase):
    async def test_a_writer_affects_the_readers_of_its_table(self):
        names = {
            n.properties["name"]: n.properties["qualified_name"]
            for n in self.outputs[0].nodes
            if "Symbol" in n.labels
        }
        impact = await self.service.get_impact(
            names["OrderRepository"], self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(
            [
                (
                    i["element"]["name"],
                    i["depth"],
                    i["evidence_type"],
                    [e["type"] for e in i["edges"]],
                )
                for i in impact["items"]
            ],
            [("orders", 1, "pattern_matched", ["WRITES"])],
        )
        # A reader changes nothing it reads.
        reader = await self.service.get_impact(
            names["LineItemReader"], self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(reader["items"], [])


@unittest.skipUnless(
    JAVA and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the java grammar",
)
class SharedTableTests(GraphCase):
    files = shared_datasource_files()

    async def test_tables_shared_between_packages(self):
        shared = await self.service.get_shared_tables(
            self.repo, snapshot_id=self.snapshot
        )
        self.assertEqual(
            [
                (
                    item["element"]["name"],
                    item["writers"],
                    [
                        (
                            p["qualified_name"].split("::")[1],
                            [(r["role"], r["count"]) for r in p["roles"]],
                        )
                        for p in item["packages"]
                    ],
                )
                for item in shared["items"]
            ],
            [
                (
                    "orders",
                    1,
                    [
                        (
                            "billing/pom.xml",
                            [("MAPS_TO", 1), ("READS", 1), ("WRITES", 1)],
                        ),
                        ("report/pom.xml", [("MAPS_TO", 1), ("READS", 1)]),
                    ],
                )
            ],
        )


MONGOOSE = {
    "package.json": '{"dependencies": {"mongoose": "^8.9.0"}}',
    "legacy/package.json": '{"dependencies": {"mongoose": "^5.0.0 || ^6.0.0"}}',
    "models/user.js": """\
const mongoose = require('mongoose');
const { Schema } = mongoose;
const userSchema = new Schema({ email: String });
userSchema.statics.byEmail = function (email) {
  return this.findOne({ email });
};
module.exports = mongoose.model('User', userSchema);
""",
    "models/post.ts": """\
import { Schema, model } from 'mongoose';
const base = { timestamps: true };
const postSchema = new Schema({}, { ...base, collection: 'blog_posts' });
class PostClass {
  static recent() { return this.find().limit(5); }
}
postSchema.loadClass(PostClass);
export const Post = model('Post', postSchema);
""",
    "models/event.js": """\
const mongoose = require('mongoose');
const Event = mongoose.model(
  'Event', new mongoose.Schema({}, { discriminatorKey: 'kind' }), 'audit_events');
const Click = Event.discriminator('Click', new mongoose.Schema({}));
const Odd = mongoose.model('Odd', new mongoose.Schema({}), process.env.TABLE);
const Shared = mongoose.model('Shared', require('shared-schemas').schema);
module.exports = { Event, Click, Odd, Shared };
""",
    "models/index.js": """\
module.exports = { User: require('./user'), Post: require('./post').Post };
""",
    "legacy/goose.js": """\
const mongoose = require('mongoose');
module.exports = mongoose.model('Goose', new mongoose.Schema({}));
""",
    "controllers/users.js": """\
const mongoose = require('mongoose');
const _ = require('lodash');
const User = require('../models/user');
const models = require('../models');
const { Click } = require('../models/event');
async function handle(list) {
  await User.find({}).lean();
  await models.User.updateOne({}, {});
  await models.Post.findOneAndUpdate({}, {});
  await mongoose.model('Event').countDocuments();
  await mongoose.models.Click.deleteMany({});
  await Click.create({});
  _.find(list, 'a');
  list.find((x) => x);
  await models.Missing.find();
}
""",
    "test/users.test.js": """\
const User = require('../models/user');
User.deleteMany({});
""",
}


MONGOOSE_REPOSITORIES = {
    "libs/dal/package.json": '{"name": "@acme/dal", "main": "dist/index.js", '
    '"types": "dist/index.d.ts", "dependencies": {"mongoose": "^8.0.0"}}',
    "libs/dal/tsconfig.build.json": """\
{
  // the build writes src/ to dist/
  "compilerOptions": {"outDir": "./dist", "rootDir": "./src"}
}
""",
    "libs/dal/src/index.ts": """\
export * from './base';
export * from './user';
export * from './audit';
""",
    "libs/dal/src/base.ts": """\
import { Model } from 'mongoose';

export class BaseRepository<T> {
  public _model: Model<T>;
  constructor(protected MongooseModel: Model<T>, protected entity?: unknown) {
    this._model = MongooseModel;
  }
  find(query) { return this.MongooseModel.find(query); }
  async findOne(query) { return await this.MongooseModel.findOne<T>(query); }
  async create(data) {
    const doc = new this.MongooseModel(data);
    return doc.save();
  }
  update(query, body) { return this._model.updateMany(query, body); }
  upsert(query) { return this.update(query, {}); }
  withTransaction() { return this._model.db.startSession(); }
}
""",
    "libs/dal/src/user.ts": """\
import { model, Schema } from 'mongoose';
import { BaseRepository } from './base';

export const User = model('User', new Schema({ email: String }));

export class UserRepository extends BaseRepository<unknown> {
  constructor() {
    super(User, Object);
  }
  findByEmail(email) { return this.MongooseModel.findOne({ email }); }
  touch() { return this.upsert({}); }
}

export {
  // a comment in the braces is not an export
  UserRepository as Users,
};
""",
    "libs/dal/src/audit.ts": """\
import mongoose from 'mongoose';
import { BaseRepository } from './base';

const Audit = mongoose.model('Audit', new mongoose.Schema({}), 'audit_log');

export class AuditRepository extends BaseRepository<unknown> {
  constructor() {
    super(Audit);
  }
}
""",
    "apps/api/package.json": '{"name": "api", "dependencies": {"@acme/dal": "workspace:*"}}',
    "apps/api/src/users.service.ts": """\
import { Logger } from '@nestjs/common';
import { AuditRepository, UserRepository } from '@acme/dal';

export class UsersService {
  private audit = new AuditRepository();
  constructor(private readonly users: UserRepository, private logger: Logger) {}
  get() { return this.users.findOne({}); }
  add() { return this.users.create({}); }
  byEmail() { return this.users.findByEmail('a'); }
  run() { return this.users.withTransaction(); }
  log() { return this.audit.update({}, {}); }
  refresh() { return this.users.upsert({}); }
  say() { this.logger.log('x'); }
}
""",
    "apps/other/package.json": '{"name": "other", "dependencies": {"@acme/dal": "^1.0.0"}}',
    "apps/other/src/x.ts": """\
import { UserRepository } from '@acme/dal';

export class X {
  constructor(private users: UserRepository) {}
  get() { return this.users.findOne({}); }
}
""",
}


NEST_MONGOOSE = {
    "package.json": '{"dependencies": {"mongoose": "^8.0.0"}}',
    "src/schema.options.ts": "export const base = { timestamps: true, collection: 'audit' };\n",
    "src/cats/cat.schema.ts": """\
import { Prop, Schema, SchemaFactory } from '@nestjs/mongoose';
@Schema({ timestamps: true })
export class Cat {
  @Prop() name: string;
}
export const CatSchema = SchemaFactory.createForClass(Cat);
""",
    "src/dogs/dog.schema.ts": """\
import { Schema, SchemaFactory } from '@nestjs/mongoose';
@Schema({ collection: 'canines' })
export class Dog {}
export const DogSchema = SchemaFactory.createForClass(Dog);
""",
    "src/audit/entry.schema.ts": """\
import { Schema } from 'mongoose';
import { base } from '../schema.options';
export const entrySchema = new Schema({}, base);
""",
    "src/cats/cats.module.ts": """\
import { MongooseModule } from '@nestjs/mongoose';
import { Cat, CatSchema } from './cat.schema';
import { Dog, DogSchema } from '../dogs/dog.schema';
import { entrySchema } from '../audit/entry.schema';
export const imports = [
  MongooseModule.forFeature([
    { name: Cat.name, schema: CatSchema },
    { name: Dog.name, schema: DogSchema },
    { name: 'Entry', schema: entrySchema },
  ]),
];
""",
    "src/dogs/dogs.module.ts": """\
import { MongooseModule } from '@nestjs/mongoose';
import { Cat, CatSchema } from '../cats/cat.schema';
export const imports = [MongooseModule.forFeature([{ name: Cat.name, schema: CatSchema }])];
""",
    "src/cats/cats.service.ts": """\
import { InjectModel } from '@nestjs/mongoose';
import { Model } from 'mongoose';
import { Cat } from './cat.schema';
export class CatsService {
  constructor(
    @InjectModel(Cat.name) private readonly catModel: Model<Cat>,
    @InjectModel('Dog') private dogs: Model<unknown>,
    private readonly other: OtherService,
  ) {}
  async all() {
    await this.catModel.find().exec();
    await this.dogs.updateOne({}, {});
    await this.other.find();
  }
}
""",
}


class MongooseTests(unittest.TestCase):
    def test_repository_classes_through_a_workspace_package(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, MONGOOSE_REPOSITORIES)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        rows = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
                r.properties.get("method", ""),
                r.source_match["qualified_name"].split("::")[1],
                r.properties.get("line"),
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        service = "apps/api/src/users.service.ts"
        user = "libs/dal/src/user.ts"
        self.assertEqual(
            rows,
            [
                ("READS", "users", "mongoose-repository-call", "", user, 10),
                ("READS", "users", "mongoose-repository-call", "findOne", service, 7),
                ("WRITES", "audit_log", "mongoose-repository-call", "update", service, 11),
                ("WRITES", "users", "mongoose-repository-call", "create", service, 8),
                ("WRITES", "users", "mongoose-repository-call", "upsert", service, 12),
                ("WRITES", "users", "mongoose-repository-call", "upsert", user, 11),
            ],
        )  # fmt: skip
        self.assertEqual(
            output.metadata.custom_metrics["mongoose_repository_resolution"],
            {
                "declared_method": 1,
                "linked": 6,
                "no_model_access": 1,
                "unresolved_model": 0,
            },
        )

    def test_nest_models_injected_models_and_imported_options(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, NEST_MONGOOSE)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        rows = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
                r.properties.get("model"),
                r.source_match["qualified_name"].split("::")[1],
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        cats = "src/cats/cats.module.ts"
        self.assertEqual(
            rows,
            [
                ("MAPS_TO", "audit", "mongoose-collection", "Entry", cats),
                ("MAPS_TO", "canines", "mongoose-collection", "Dog", cats),
                ("MAPS_TO", "cats", "mongoose-default-collection", "Cat", cats),
                ("MAPS_TO", "cats", "mongoose-default-collection", "Cat", "src/dogs/dogs.module.ts"),
                ("READS", "cats", "mongoose-call", "Cat", "src/cats/cats.service.ts"),
                ("WRITES", "canines", "mongoose-call", "Dog", "src/cats/cats.service.ts"),
            ],
        )  # fmt: skip
        self.assertEqual(
            output.metadata.custom_metrics["mongoose_access_resolution"],
            {"linked": 2, "unresolved_model": 0},
        )

    def test_default_names_match_mongoose(self):
        cases = {
            "User": "users", "UserHistory": "userhistories", "Person": "people",
            "Woman": "women", "Child": "children", "Ox": "oxen", "Axis": "axes",
            "Octopus": "octopi", "Status": "status", "Bus": "buses",
            "Tomato": "tomatoes", "Datum": "data", "Analysis": "analyses",
            "Leaf": "leafs", "Wolf": "wolves", "Knife": "knives", "Hive": "hives",
            "Category": "categories", "Box": "boxes", "Index": "indexes",
            "Mouse": "mice", "Quiz": "quizzes", "Fish": "fish", "News": "news",
            "Address": "addresses", "Photo": "photos", "Item2": "item2",
            "system.profile": "system.profile",
        }  # fmt: skip
        for name, plural in cases.items():
            self.assertEqual(mongoose_pluralize(name), plural, name)
        self.assertEqual(mongoose_pluralize("Goose", 5), "gooses")
        self.assertEqual(mongoose_pluralize("Goose", 6), "geese")
        self.assertEqual(mongoose_pluralize("Human", 6), "humen")
        self.assertEqual(mongoose_pluralize("Human", 7), "humans")
        self.assertEqual(mongoose_majors(["^8.23.0"]), {7})
        self.assertEqual(mongoose_majors(["~5.13.2"]), {5})
        self.assertEqual(mongoose_majors([">=6 <8"]), {6, 7})
        self.assertEqual(mongoose_majors(["latest"]), {5, 6, 7})
        self.assertEqual(mongoose_majors([]), {5, 6, 7})

    def test_factories_typed_receivers_and_schema_without_new(self):
        files = {
            "models.ts": """import mongoose, { Mongoose } from 'mongoose';
const COLLECTION = 'conformances';
let renamed = 'a';
const plain = mongoose.Schema({}, { collection: 'plain_items' });
export const Plain = mongoose.model('Plain', plain);
export function createUser(m: typeof import('mongoose')) {
  return m.models.User || m.model('User', new m.Schema({}));
}
export function createTeam(mg: typeof mongoose) {
  return mg.model('Team', plain);
}
export const providers = [
  { useFactory: (db: Mongoose) => db.model('Cat', new mongoose.Schema({})) },
];
let Kept;
Kept = mongoose.model('Kept', new mongoose.Schema({}), COLLECTION);
export const Varied = mongoose.model('Varied', plain, renamed);
// A name typed as mongoose in one function and not in another is left out.
export function shadowed(x: typeof mongoose) {
  return x.model('Shadowed', plain);
}
export function notMongoose(x: typeof Other) {
  return x.model('Other', plain);
}
""",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["typescript"])
            )
        rows = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
            )
            for r in output.relationships
            if r.type == "MAPS_TO"
        )
        self.assertEqual(
            rows,
            [
                ("createTeam", "plain_items", "mongoose-collection"),
                ("createUser", "users", "mongoose-default-collection"),
                ("file", "cats", "mongoose-default-collection"),
                ("file", "conformances", "mongoose-collection"),
                ("file", "plain_items", "mongoose-collection"),
            ],
        )

    def test_package_dependency_gates_and_assigned_handles(self):
        files = {
            "package.json": '{"dependencies": {"mongodb": "6.0.0"}}',
            "src/mongo.js": """const connection = require('./connection');
let db;
async function init(mod) {
  const client = await connection.connect();
  db = client.db();
  const shared = mod.db || db;
  await db.collection('objects').createIndex({ key: 1 });
  await shared.collection('objects').ensureIndex({ key: 1 });
  return db.collection('objects').initializeUnorderedBulkOp();
}
""",
            "src/jobs.js": """class Manager {
  start(common) {
    this.collection = common.db.collection('jobs');
  }
  next() {
    return this.collection.find({}).toArray();
  }
}
""",
            "src/cache.js": """let store;
store = new Map();
store.collection('ignored');
""",
            "web/package.json": "{}",
            "web/other.js": "function f(x) { x.db.collection('below'); }\n",
            "lib/package.json": '{"name": "lib"}',
        }
        other = {
            "package.json": "{}",
            "src/plain.js": "function f(x) { x.db.collection('ungated'); }\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["javascript"])
            )
        with tempfile.TemporaryDirectory() as directory:
            write(directory, other)
            ungated = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["javascript"])
            )
        edges = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[1],
                r.properties["collection"],
                r.properties["line"],
            )
            for r in output.relationships
            if r.type in ("READS", "WRITES")
        )
        # No file imports mongodb; the root package.json depends on it, and
        # Node resolves it from there for web/ too. A name assigned only
        # handles is one; store is assigned a Map.
        self.assertEqual(
            edges,
            [
                ("READS", "src/jobs.js", "jobs", 3),
                ("READS", "web/other.js", "below", 1),
                ("WRITES", "src/mongo.js", "objects", 7),
                ("WRITES", "src/mongo.js", "objects", 8),
                ("WRITES", "src/mongo.js", "objects", 9),
                ("WRITES", "web/other.js", "below", 1),
            ],
        )
        self.assertEqual(
            [r for r in ungated.relationships if r.type in ("READS", "WRITES")], []
        )

    def test_driver_collections_by_name(self):
        files = {
            "package.json": "{}",
            "models/user.js": """const mongoose = require('mongoose');
module.exports = mongoose.model('User', new mongoose.Schema({}));
""",
            "scripts/migrate.ts": """import mongoose from 'mongoose';
import { MongoClient, Db } from 'mongodb';
const client = new MongoClient('x');
export async function migrate() {
  const db = client.db('app');
  await db.collection('users').updateMany({}, {});
  const projects = db.collection('projects');
  await projects.find({}).toArray();
  await mongoose.connection.db!.collection('sharedlinks').drop();
  await mongoose.connection.collection('logs').countDocuments();
  const both = db.collection('audit');
  await both.insertOne({});
  await both.findOne({});
}
export function watch(handle: Db) {
  return handle.collection('roles').watch([]);
}
export function untyped(thing) {
  return thing.collection('untyped').find();
}
export function dynamic(name: string) {
  return client.db('app').collection(name).find();
}
""",
            "scripts/typed.ts": """export function changes(m: typeof import('mongoose')) {
  const db = m.connection.db;
  return db!.collection('roles').watch([]);
}
""",
            "firestore.ts": """import { getFirestore } from 'firebase/firestore';
const db = getFirestore();
export const users = () => db.collection('users').get();
""",
        }
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["javascript", "typescript"]
                )
            )
        rows = sorted(
            (
                r.type,
                r.source_match["qualified_name"].split("::")[2],
                r.target_match["qualified_name"].rsplit("::", 1)[1],
            )
            for r in output.relationships
            if r.properties.get("rule_id") == "data-contracts.mongodb-collection"
        )
        self.assertEqual(
            rows,
            [
                ("READS", "changes", "roles"),
                ("READS", "migrate", "audit"),
                ("READS", "migrate", "logs"),
                ("READS", "migrate", "projects"),
                ("READS", "watch", "roles"),
                ("WRITES", "migrate", "audit"),
                ("WRITES", "migrate", "sharedlinks"),
                ("WRITES", "migrate", "users"),
            ],
        )
        # The driver's users collection is the User model's table.
        users = {
            r.target_match["qualified_name"]
            for r in output.relationships
            if r.target_match.get("qualified_name", "").endswith("::users")
        }
        self.assertEqual(len(users), 1)
        self.assertEqual(
            output.metadata.custom_metrics["mongodb_collections"],
            {"linked": 7},
        )

    def test_models_collections_and_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, MONGOOSE)
            output = asyncio.run(
                DataContractTool(directory).analyze(
                    "r", languages=["javascript", "typescript"]
                )
            )
        rows = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
                r.properties.get("model"),
                r.properties.get("line"),
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        self.assertEqual(
            rows,
            [
                ("MAPS_TO", "audit_events", "mongoose-collection", "Click", 4),
                ("MAPS_TO", "audit_events", "mongoose-collection", "Event", 2),
                ("MAPS_TO", "blog_posts", "mongoose-collection", "Post", 8),
                ("MAPS_TO", "users", "mongoose-default-collection", "User", 7),
                ("READS", "audit_events", "mongoose-call", "Event", 10),
                ("READS", "blog_posts", "mongoose-call", "Post", 5),
                ("READS", "blog_posts", "mongoose-call", "Post", 9),
                ("READS", "users", "mongoose-call", "User", 5),
                ("READS", "users", "mongoose-call", "User", 7),
                ("WRITES", "audit_events", "mongoose-call", "Click", 11),
                ("WRITES", "audit_events", "mongoose-call", "Click", 12),
                ("WRITES", "blog_posts", "mongoose-call", "Post", 9),
                ("WRITES", "users", "mongoose-call", "User", 8),
            ],
        )
        discriminator = next(
            r
            for r in output.relationships
            if r.type == "MAPS_TO" and r.properties.get("model") == "Click"
        )
        self.assertEqual(discriminator.properties["discriminator_of"], "Event")
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["mongoose_models"], 4)
        self.assertEqual(
            metrics["mongoose_unresolved"],
            {"collection": 1, "schema": 1, "version": 1},
        )
        self.assertEqual(
            metrics["mongoose_access_resolution"],
            {"linked": 8, "not_a_model": 1, "unresolved_model": 0},
        )


DOCTRINE = {
    "composer.lock": '{"packages": [{"name": "doctrine/orm", "version": "2.20.1"}]}',
    "config/packages/doctrine.yaml": """\
doctrine:
    orm:
        naming_strategy: doctrine.orm.naming_strategy.underscore
""",
    "config/doctrine/Invoice.orm.xml": """\
<doctrine-mapping xmlns="http://doctrine-project.org/schemas/orm/doctrine-mapping">
  <entity name="App\\Entity\\Invoice" table="invoices"/>
</doctrine-mapping>
""",
    "src/Entity/Invoice.php": """\
<?php
namespace App\\Entity;
class Invoice {}
""",
    "src/Entity/BlogPost.php": """\
<?php
namespace App\\Entity;
use App\\Repository\\BlogPostRepository;
use Doctrine\\ORM\\Mapping as ORM;
#[ORM\\Entity(repositoryClass: BlogPostRepository::class)]
class BlogPost
{
    #[ORM\\ManyToMany(targetEntity: Tag::class)]
    #[ORM\\JoinTable(name: 'blog_post_tags')]
    private $tags;
}
""",
    "src/Entity/Oauth2Client.php": """\
<?php
namespace App\\Entity;
use Doctrine\\ORM\\Mapping\\Entity;
#[Entity]
class Oauth2Client {}
""",
    "src/Entity/Person.php": """\
<?php
namespace App\\Entity;
use Doctrine\\ORM\\Mapping as ORM;
/**
 * @ORM\\Entity
 * @ORM\\Table(name="people", indexes={@ORM\\Index(name="idx", columns={"a"})})
 * @ORM\\InheritanceType("SINGLE_TABLE")
 */
class Person {}
""",
    "src/Entity/Employee.php": """\
<?php
namespace App\\Entity;
use Doctrine\\ORM\\Mapping as ORM;
/** @ORM\\Entity */
class Employee extends Person {}
""",
    "src/Entity/Base.php": """\
<?php
namespace App\\Entity;
use Doctrine\\ORM\\Mapping as ORM;
#[ORM\\MappedSuperclass]
abstract class Base {}
#[ORM\\Entity]
#[ORM\\Table(name: 'comments')]
class Comment extends Base {}
""",
    "src/Repository/BlogPostRepository.php": """\
<?php
namespace App\\Repository;
use App\\Entity\\BlogPost;
use Doctrine\\Bundle\\DoctrineBundle\\Repository\\ServiceEntityRepository;
class BlogPostRepository extends ServiceEntityRepository
{
    public function __construct($registry)
    {
        parent::__construct($registry, BlogPost::class);
    }
    public function findLatest()
    {
        return $this->createQueryBuilder('p')->getQuery()->getResult();
    }
    public function findByTag($tag)
    {
        return $this->findBy(['tag' => $tag]);
    }
}
""",
    "src/Controller/PostController.php": """\
<?php
namespace App\\Controller;
use App\\Entity\\Comment;
use App\\Entity\\Employee;
use App\\Entity\\Invoice;
use App\\Entity\\Oauth2Client;
use App\\Entity\\Person;
use App\\Repository\\BlogPostRepository;
use App\\Settings\\SystemConfig;
class PostController
{
    public function __construct(private BlogPostRepository $posts, private $em) {}
    public function index(SystemConfig $config)
    {
        $this->posts->findAll();
        $this->posts->findByTag('x');
        $this->em->getRepository(Person::class)->findOneBy([]);
        $this->em->find(Invoice::class, 1);
        $config->find('key');
    }
    public function save(Comment $comment, $qb)
    {
        $this->em->persist($comment);
        $employee = new Employee();
        $this->em->remove($employee);
        $qb->update(Oauth2Client::class, 'o')->getQuery()->execute();
        $dql = 'SELECT p FROM ' . Person::class . ' p JOIN p.manager m';
        $this->em->createQuery($dql . ' WHERE p.id IN (SELECT c.id FROM AppBundle:Comment c)');
        $this->em->createQuery('DELETE FROM App\\\\Entity\\\\Invoice i');
    }
}
""",
    "src/AppBundle.php": """\
<?php
namespace App;
class AppBundle {}
""",
    "tests/PostTest.php": """\
<?php
$this->em->find(App\\Entity\\Invoice::class, 1);
""",
}


class DoctrineTests(unittest.TestCase):
    def test_default_names_follow_the_naming_strategy(self):
        from deepthought.data_contracts import doctrine_strategies, doctrine_table_name

        self.assertEqual(
            doctrine_table_name("App\\Entity\\BlogPost", "default"), "BlogPost"
        )
        self.assertEqual(
            doctrine_table_name("App\\BlogPost", "underscore"), "blog_post"
        )
        self.assertEqual(
            doctrine_table_name("Oauth2Client", "underscore", number_aware=False),
            "oauth2client",
        )
        self.assertEqual(
            doctrine_table_name("Oauth2Client", "underscore"), "oauth2_client"
        )
        underscore = ["doctrine.orm.naming_strategy.underscore"]
        self.assertEqual(
            doctrine_strategies(underscore, [], {3}), {("underscore", True)}
        )
        self.assertEqual(
            doctrine_strategies(underscore, [], set()),
            {("underscore", False), ("underscore", True)},
        )
        self.assertEqual(doctrine_strategies([], [], set()), {("default", True)})
        self.assertEqual(
            doctrine_strategies(
                [],
                [
                    "$c->setNamingStrategy(new UnderscoreNamingStrategy(CASE_LOWER, true));"
                ],
                {2},
            ),
            {("underscore", True)},
        )
        self.assertIs(doctrine_strategies(["app.naming"], [], {3}), UNKNOWN)

    def test_entities_repositories_and_entity_manager_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DOCTRINE)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["php"])
            )
        rows = sorted(
            (
                r.type,
                r.target_match["qualified_name"].rsplit("::", 1)[1],
                r.properties["rule_id"].split(".", 1)[1],
                r.properties["entity"].rpartition("\\")[2],
                r.source_match["qualified_name"].split("::")[1],
                r.properties.get("line"),
            )
            for r in output.relationships
            if r.type != "CONTAINS"
        )
        post = "src/Controller/PostController.php"
        repository = "src/Repository/BlogPostRepository.php"
        self.assertEqual(
            rows,
            [
                ("MAPS_TO", "blog_post", "doctrine-default-table", "BlogPost", "src/Entity/BlogPost.php", 6),
                ("MAPS_TO", "blog_post_tags", "doctrine-table", "BlogPost", "src/Entity/BlogPost.php", 8),
                ("MAPS_TO", "comments", "doctrine-table", "Comment", "src/Entity/Base.php", 8),
                ("MAPS_TO", "invoices", "doctrine-table", "Invoice", "src/Entity/Invoice.php", 3),
                ("MAPS_TO", "oauth2client", "doctrine-default-table", "Oauth2Client", "src/Entity/Oauth2Client.php", 5),
                ("MAPS_TO", "people", "doctrine-table", "Employee", "src/Entity/Employee.php", 5),
                ("MAPS_TO", "people", "doctrine-table", "Person", "src/Entity/Person.php", 9),
                ("READS", "blog_post", "doctrine-call", "BlogPost", post, 15),
                ("READS", "blog_post", "doctrine-call", "BlogPost", repository, 13),
                ("READS", "blog_post", "doctrine-call", "BlogPost", repository, 17),
                ("READS", "comments", "doctrine-dql", "Comment", post, 28),
                ("READS", "invoices", "doctrine-call", "Invoice", post, 18),
                ("READS", "people", "doctrine-call", "Person", post, 17),
                ("READS", "people", "doctrine-dql", "Person", post, 28),
                ("WRITES", "comments", "doctrine-call", "Comment", post, 23),
                ("WRITES", "invoices", "doctrine-dql", "Invoice", post, 29),
                ("WRITES", "oauth2client", "doctrine-call", "Oauth2Client", post, 26),
                ("WRITES", "people", "doctrine-call", "Employee", post, 25),
            ],
        )  # fmt: skip
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["doctrine_entities"], 6)
        self.assertEqual(metrics["doctrine_unresolved"], {})
        self.assertEqual(
            metrics["doctrine_access_resolution"],
            {"linked": 11, "not_an_entity": 0, "unresolved_entity": 0},
        )

    def test_metadata_listener_leaves_tables_unresolved(self):
        files = dict(DOCTRINE)
        files["src/EventSubscriber/Prefix.php"] = """\
<?php
class Prefix
{
    public function loadClassMetadata($args)
    {
        $args->getClassMetadata()->setPrimaryTable(['name' => 'app_' . $name]);
    }
}
"""
        with tempfile.TemporaryDirectory() as directory:
            write(directory, files)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["php"])
            )
        self.assertFalse([r for r in output.relationships if r.type != "CONTAINS"])
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["doctrine_unresolved"], {"metadata_listener": 6})
        self.assertEqual(metrics["doctrine_access_resolution"]["unresolved_entity"], 11)


if __name__ == "__main__":
    unittest.main()
