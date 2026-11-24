"""Database tables: SQL DDL, JPA entity mappings and Spring Data repositories."""

import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from deepthought.analysis_tools import (
    ComponentsTool,
    DataContractTool,
    TreeSitterTool,
)
from deepthought.analysis_tools.evidence import get_rule
from deepthought.data_contracts import default_table_name, extract_ddl
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinatorConfig,
    _combine,
)

JAVA = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_java"
)

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


@unittest.skipUnless(
    JAVA and os.environ.get("DEEPTHOUGHT_NEO4J_URI"),
    "Provision isolated Neo4j and the java grammar",
)
class ImpactTests(unittest.IsolatedAsyncioTestCase):
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
            write(directory, WORKSPACE)
            self.outputs = [
                await TreeSitterTool(directory).analyze(self.repo, languages={"java"}),
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


if __name__ == "__main__":
    unittest.main()
