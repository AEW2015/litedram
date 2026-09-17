#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""External query failures must not fall back to stale local connectivity."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from litedram.phy.usnative.query import query_device, tcl_path
from test.test_usnative_topology import topology_fixture


class TestUSNativeQuery(unittest.TestCase):
    def test_missing_tool_fails_before_creating_output(self):
        pins, _ = topology_fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'new'
            with patch('litedram.phy.usnative.query.shutil.which', return_value=None):
                with self.assertRaises(OSError):
                    query_device(pins, output)
            self.assertFalse(output.exists())

    def test_failed_query_never_reads_stale_map(self):
        pins, _ = topology_fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            stale = output / 'physical.tsv'
            stale.write_text('stale device data')
            with patch('litedram.phy.usnative.query.shutil.which', return_value='vivado'), \
                 patch('litedram.phy.usnative.query.subprocess.run',
                       return_value=SimpleNamespace(returncode=1)) as run:
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'query failed'):
                        query_device(pins, output)
                self.assertEqual(run.call_count, 2)
            self.assertEqual(stale.read_text(), 'stale device data')
            self.assertEqual(len(list(output.glob('usnative-query-*'))), 2)

    def test_tcl_path_rejects_delimiter_injection(self):
        for value in ('query{bad}', 'query\nexit'):
            with self.assertRaises(ValueError):
                tcl_path(value)
