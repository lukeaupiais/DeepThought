from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import knex_labels  # noqa: E402

SOURCE = """
import type { Knex } from 'knex';

export async function up(knex) {
  await knex.schema.createTable('posts', (t) => t.string('id'));
  await knex.schema.alterTable('tags', (t) => t.string('id'));
  await knex('posts').where({ id: 1 }).update({ title: 'x' });
  const rows = await knex('posts as p')
    .join('tags', 'tags.id', 'p.tag_id')
    .select('p.id');
  await knex.insert({ id: 1 }).into('tags');
  await knex.batchInsert('posts_tags', rows, 100);
  await knex.with('recent', knex('posts').select('id')).select('*').from('recent');
  const held = knex('members').where({ id: 2 });
  const found = await held.first();
  await held.del();
  const sql = knex('members').toString();
  await other.from('posts');
}

class Repo {
  constructor(private executor: Knex) {}
  read() {
    return this.executor('settings').first();
  }
}

import { TAGS } from './tables';
const POSTS = 'posts';
export async function down(knex) {
  await knex.transaction(async (tx) => {
    await tx(POSTS).whereIn('id', (q) => q.select('post_id').from(TAGS)).del();
  });
}
const recent = (q: Knex.QueryBuilder) => q.from('members').select('id');
const catalog = () => knex('information_schema.tables').select('table_name');
const qualified = () => knex('app.audit').insert({ id: 1 });
"""


class KnexLabelsTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        (self.repo / "m.ts").write_text(SOURCE, "utf-8")
        (self.repo / "tables.ts").write_text("export const TAGS = 'tags';\n", "utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def sites(self):
        labels, unresolved, ignored = knex_labels.label(
            self.repo, set(knex_labels.HANDLES), set(), {}, set(), ()
        )
        return (
            {(i["line"], i["table"], tuple(i["access"])) for i in labels},
            {(i["line"], i["table"]) for i in unresolved},
            {(i["line"], i["table"]) for i in ignored},
        )

    def test_knex_semantics(self):
        labels, unresolved, ignored = self.sites()
        self.assertEqual(
            labels,
            {
                (5, "posts", ("DECLARES",)),
                (7, "posts", ("WRITES",)),
                (8, "posts", ("READS",)),
                (9, "tags", ("READS",)),
                (11, "tags", ("WRITES",)),
                (12, "posts_tags", ("WRITES",)),
                # The CTE `recent` is no table; its body reads posts.
                (13, "posts", ("READS",)),
                # Held, then read once and deleted once.
                (14, "members", ("READS", "WRITES")),
                # A parameter property typed Knex is a handle.
                (24, "settings", ("READS",)),
                # The transaction a callback gets is a handle; a table may be
                # a constant, imported or not; a subquery callback reads.
                (32, "posts", ("WRITES",)),
                (32, "tags", ("READS",)),
                # A builder typed Knex.QueryBuilder.
                (35, "members", ("READS",)),
                # A schema qualifier is dropped.
                (37, "audit", ("WRITES",)),
            },
        )
        # Schema calls, and a database's own catalog.
        self.assertEqual(ignored, {(6, "tags"), (36, "tables")})
        # Only SQL text: given by hand.
        self.assertEqual(unresolved, {(17, "members")})


if __name__ == "__main__":
    unittest.main()
