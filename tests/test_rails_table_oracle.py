"""tools/rails_table_oracle.rb on a small app.

Skipped unless Ruby with Prism and the activerecord gem is installed: the
labels it produces are committed, so CI does not run it.
"""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ORACLE = Path(__file__).resolve().parents[1] / "tools" / "rails_table_oracle.rb"


def _available():
    if shutil.which("ruby") is None:
        return False
    probe = subprocess.run(
        ["ruby", "-e", 'require "prism"; require "active_record"'],
        capture_output=True,
    )
    return probe.returncode == 0


FILES = {
    "app/models/application_record.rb": (
        "class ApplicationRecord < ActiveRecord::Base\n  primary_abstract_class\nend\n"
    ),
    "app/models/person.rb": "class Person < ApplicationRecord\nend\n",
    "app/models/admin.rb": "class Admin < Person\nend\n",
    "app/models/users/setting.rb": (
        "module Users\n  class Setting < ApplicationRecord\n"
        '    self.table_name_prefix = "users_"\n  end\nend\n'
    ),
    "app/models/billing.rb": (
        'module Billing\n  def self.table_name_prefix\n    "billing_"\n  end\nend\n'
    ),
    "app/models/billing/invoice.rb": (
        "class Billing::Invoice < ApplicationRecord\nend\n"
    ),
    "app/models/room.rb": (
        'class Room < ApplicationRecord\n  self.table_name = "#{prefix}rooms"\nend\n'
    ),
    "app/models/octopus.rb": "class Octopus < ApplicationRecord\nend\n",
    "config/initializers/inflections.rb": (
        "ActiveSupport::Inflector.inflections(:en) do |inflect|\n"
        '  inflect.irregular "octopus", "octopi"\nend\n'
    ),
    "spec/support/fake.rb": "class Fake < ApplicationRecord\nend\n",
    "vendor/gem/model.rb": "class Vendored < ApplicationRecord\nend\n",
}


@unittest.skipUnless(_available(), "Ruby with Prism and activerecord")
class OracleTests(unittest.TestCase):
    def test_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in FILES.items():
                (Path(tmp) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / name).write_text(text, "utf-8")
            out = subprocess.run(
                ["ruby", str(ORACLE), tmp], capture_output=True, text=True, check=True
            )
        result = json.loads(out.stdout)
        self.assertEqual(
            {m["class"]: m["table"] for m in result["models"]},
            {
                "Person": "people",
                "Admin": "people",  # single-table inheritance
                "Users::Setting": "users_settings",
                "Billing::Invoice": "billing_invoices",
                "Octopus": "octopi",
            },
        )
        self.assertEqual(
            {m["class"]: m["reason"] for m in result["not_counted"]},
            {"Room": "table_name_not_literal", "Fake": "test_only"},
        )


if __name__ == "__main__":
    unittest.main()
