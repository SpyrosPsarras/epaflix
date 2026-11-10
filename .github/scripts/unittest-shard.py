#!/usr/bin/env python3
"""Run every COUNT-th test of a unittest script, starting at INDEX: unittest-shard.py SCRIPT INDEX COUNT."""
import importlib.util
import os
import sys
import unittest

script, index, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
sys.argv = [script]
spec = importlib.util.spec_from_file_location("shard_module", script)
module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def flatten(suite):
    for item in suite:
        yield from flatten(item) if isinstance(item, unittest.TestSuite) else [item]


tests = list(flatten(unittest.TestLoader().loadTestsFromModule(module)))[index::count]
if not tests:
    sys.exit(f"no tests in shard {index} of {count} for {script}")
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(tests))
sys.exit(not result.wasSuccessful())
