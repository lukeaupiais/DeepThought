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
from deepthought.data_contracts import (
    default_table_name,
    extract_ddl,
    statement_tables,
)
from deepthought.service_contracts.spring import _text_block
from deepthought.storage.graph.write_coordinator import (
    WriteCoordinatorConfig,
    _combine,
)

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


@unittest.skipUnless(PYTHON, "Provision the python grammar")
class DjangoTests(unittest.TestCase):
    def test_models_tables_and_manager_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, DJANGO)
            output = asyncio.run(
                DataContractTool(directory).analyze("r", languages=["python"])
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
        self.assertEqual(
            edges,
            [
                ("MAPS_TO", "Line", "sales_line", "django-default-table-name", None),
                ("MAPS_TO", "Order", "store_order", "django-default-table-name", None),
                (
                    "MAPS_TO",
                    "Order",
                    "store_order_tags",
                    "django-many-to-many-table",
                    "tags",
                ),
                ("MAPS_TO", "Tag", "shop_tags", "django-db-table", None),
                ("READS", "close", "shop_tags", "django-manager-call", None),
                ("READS", "close", "store_order", "django-manager-call", None),
                ("READS", "shop/views.py", "store_order", "django-manager-call", None),
                ("READS", "urgent", "store_order", "django-manager-call", None),
                ("WRITES", "close", "shop_tags", "django-manager-call", None),
                ("WRITES", "close", "store_order", "django-manager-call", None),
            ],
        )
        metrics = output.metadata.custom_metrics
        self.assertEqual(metrics["django_models"], 4)
        self.assertEqual(
            metrics["django_access_resolution"], {"linked": 4, "not_a_model": 1}
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
            {"linked": 3, "not_a_mapped_class": 1},
        )


class StatementTests(unittest.TestCase):
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
            "UPDATE owners SET city = 'FROM x' WHERE id = ? FOR UPDATE": (
                (),
                ("owners",),
            ),
            "DELETE FROM pets WHERE id IN (SELECT pet FROM visits)": (
                ("visits",),
                ("pets",),
            ),
            "WITH recent (id) AS (SELECT id FROM visits) SELECT * FROM recent": (
                ("visits",),
                (),
            ),
            "SELECT EXTRACT(YEAR FROM born) FROM pets, owners o -- FROM vets": (
                ("pets", "owners"),
                (),
            ),
            "MERGE INTO stock s USING deliveries d ON s.id = d.id": (
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
            {"statements": 9, "not_fixed": 1, "unknown_entity": 1},
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
            {"statements": 6, "not_fixed": 1, "unknown_entity": 0},
        )


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
