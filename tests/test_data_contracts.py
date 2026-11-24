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


TYPESCRIPT = importlib.util.find_spec("tree_sitter") and importlib.util.find_spec(
    "tree_sitter_typescript"
)
SCRIPT_ORMS = {
    "package.json": '{"name": "shop"}',
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


@unittest.skipUnless(TYPESCRIPT, "Install the typescript grammar")
class ScriptOrmTests(unittest.TestCase):
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
                (
                    "WRITES",
                    "AuditService.constructor",
                    "audit_events",
                    "typeorm-repository",
                ),
                (
                    "WRITES",
                    "AuditService.logs",
                    "http_request_log",
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
            metrics["orm_access_resolution"],
            {
                "prisma_linked": 3,
                "prisma_ambiguous": 0,
                "typeorm_linked": 2,
                "typeorm_not_an_entity": 1,
            },
        )
        for relationship in output.relationships:
            self.assertIsNotNone(get_rule(relationship.properties["rule_id"]))


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


@unittest.skipUnless(GO, "Install the go grammar")
class GoSqlTests(unittest.TestCase):
    def test_sql_text_and_gorm_table_names(self):
        with tempfile.TemporaryDirectory() as directory:
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
            {"statements": 3, "not_fixed": 1, "unknown_entity": 0},
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


GORM = {
    "go.mod": "module example.com/shop\n\ngo 1.22\n",
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
    def test_default_names_match_gorm(self):
        from deepthought.data_contracts import gorm_table_name

        # Outputs of gorm.io/gorm 1.25.12 schema.NamingStrategy{}.TableName.
        for name, table in (
            ("UserModel", "user_models"),
            ("HTTPSServer", "http_s_servers"),
            ("Human", "humen"),
            ("APIKey", "api_keys"),
            ("UTF8Data", "utf8_data"),
            ("Person", "people"),
            ("Leaf", "leafs"),
            ("Campus", "campus"),
        ):
            self.assertEqual(gorm_table_name(name), table)

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
                ("MAPS_TO", "Legacy", "legacy_rows", "gorm-table-name"),
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


class DrizzleTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
