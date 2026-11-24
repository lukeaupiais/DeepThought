"""tools/mongodb_driver_labels.py labels driver accesses by method."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import mongodb_driver_labels as labels  # noqa: E402

SOURCE = """const db = client.db('app');
db.collection('users').findOne({ _id: 1 });
db.collection('users')
  .updateOne({ _id: 1 }, { $set: { a: 1 } });
// db.collection('old').remove({});
/* db.collection('gone').find();
   db.collection('gone').drop(); */
const jobs = db.collection('jobs');
await jobs.find({}).toArray();
await jobs.insertOne({});
db.collection('locks').findOneAndUpdate({}, {});
db.collection('odd').watchIt();
"""


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "src").mkdir()
        (self.root / "src/app.js").write_text(SOURCE, "utf-8")
        (self.root / "test").mkdir()
        (self.root / "test/app.js").write_text(SOURCE, "utf-8")

    def test_accesses_by_method_with_comments_and_tests_left_out(self):
        found, unresolved = labels.label(self.root, {}, set())
        self.assertEqual(
            [(i["line"], i["collection"], i["access"]) for i in found],
            [
                (2, "users", ["READS"]),
                (3, "users", ["WRITES"]),
                (8, "jobs", ["READS", "WRITES"]),
                (11, "locks", ["READS", "WRITES"]),
            ],
        )
        self.assertEqual(
            [(i["file"], i["line"]) for i in unresolved], [("src/app.js", 12)]
        )

    def test_override_and_skip(self):
        found, unresolved = labels.label(
            self.root, {"src/app.js:12": ["READS"]}, {"src/app.js:2"}
        )
        self.assertEqual(unresolved, [])
        self.assertEqual([i["line"] for i in found], [3, 8, 11, 12])


if __name__ == "__main__":
    unittest.main()
