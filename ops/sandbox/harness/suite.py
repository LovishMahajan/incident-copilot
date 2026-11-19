"""Artifact 11.5: run one suite with unittest and write its counts to the harness's own pipe.

    python -B /harness/suite.py <fd> <start-directory>

Run from the checkout's root. The counts go to file descriptor <fd>, not to standard output,
which the tests share; their output goes to the log.
"""

import json
import os
import sys
import unittest


def main():
    fd, start = int(sys.argv[1]), sys.argv[2]
    tests = unittest.defaultTestLoader.discover(start, top_level_dir=".")
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(tests)
    errors = [
        f"{test.id()}: {(trace.strip().splitlines() or [''])[-1][:300]}"
        for test, trace in result.failures + result.errors
    ]
    loader_errors = [
        str(e).strip().splitlines()[-1][:300] for e in unittest.defaultTestLoader.errors
    ]
    counts = {
        "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
        "failed": len(result.failures) + len(result.errors),
        "errors": errors + loader_errors,
    }
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(counts))


if __name__ == "__main__":
    main()
