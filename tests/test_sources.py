"""核对业务资料的格式与可关联性。"""
import csv
from pathlib import Path
import unittest

DATA = Path(__file__).resolve().parents[1] / 'data'
TABLES = {'rules': ('version', 'cutoff', 'scale', 'live_from', 'live_until'), 'orders': ('order', 'merchant', 'version', 'day', 'raw_score', 'price_band', 'first_purchase'), 'receipts': ('order', 'revision', 'known', 'version', 'shown', 'reason'), 'outcomes': ('order', 'revision', 'known', 'followup_days', 'state'), 'score_reviews': ('order', 'known', 'updated_score'), 'plans': ('plan', 'asof', 'version', 'cutoff', 'bandwidths', 'min_per_side', 'max_unknown', 'min_gain', 'min_jump', 'horizon', 'active_on', 'status')}
RELATION = ('receipts', 'order', 'orders', 'order')


class SourceContract(unittest.TestCase):
    def read(self, table):
        with (DATA / f'{table}.csv').open(encoding='utf-8', newline='') as file:
            return list(csv.DictReader(file))

    def test_shape(self):
        for name, columns in TABLES.items():
            with self.subTest(table=name):
                rows = self.read(name)
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual(tuple(row), columns)
                    self.assertNotIn(None, row.values())

    def test_references(self):
        left, left_key, right, right_key = RELATION
        self.assertLessEqual({row[left_key] for row in self.read(left)}, {row[right_key] for row in self.read(right)})

    def test_source_variation(self):
        self.assertEqual({row['version'] for row in self.read('receipts') if row['order'] == 'o0088'}, {'r1', 'r2'})
        scores = {int(row['raw_score']) for row in self.read('orders') if row['version'] == 'r1'}
        self.assertTrue({49, 50, 51} <= scores)
