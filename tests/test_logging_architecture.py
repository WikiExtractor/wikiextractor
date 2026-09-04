"""
Tests for wikiextractor's logging architecture: three independently
configurable, named loggers ('wikiextractor', 'wikiextractor.extract',
'wikiextractor.mapreduce'), none of which touch the root logger's own
level or handlers at all.

Before this, configure_root_logging() (as it was then named) called
logging.getLogger().setLevel(...) directly -- the root logger, shared
by definition with anything else running in the same process. A
programmatic caller embedding wikiextractor as a library, rather than
running it as a standalone CLI script, had no way to configure
wikiextractor's own verbosity independently of their own application's
logging setup, and every run of this code silently overwrote whatever
root-level configuration they'd already done.

Now: wikiextractor_logger ('wikiextractor') has its own handler and
level, propagate=False -- same established pattern mapreduce_logger
already used for exactly this reason. extract.py's own
'wikiextractor.extract' logger is a child of it by name, with no
explicit level or handler of its own, so it inherits wikiextractor's
level by default while remaining independently overridable
(logging.getLogger('wikiextractor.extract').setLevel(...) still
works, same as it always did for any Python logger hierarchy).

Both configure functions install a handler and set propagate=False on
their own logger, and both outlive the test that called them. A
handler left on 'wikiextractor' prints every record that later tests
propagate up to it, which is thousands of lines of INVOCATION/TITLE
tracing from the files that raise 'wikiextractor.extract' to DEBUG to
capture it. NamedLoggerStateMixin below snapshots level, handlers and
propagate for all three loggers so nothing survives the test.

Run with:
    python -m unittest tests.test_logging_architecture -v
or, from the tests/ directory:
    python -m unittest test_logging_architecture -v
"""

import logging
import sys
import unittest

sys.path.insert(0, '..')  # allow running directly from tests/ without installing

import wikiextractor.WikiExtractor as we


WIKIEXTRACTOR_LOGGERS = ('wikiextractor', 'wikiextractor.extract',
                          'wikiextractor.mapreduce')


class NamedLoggerStateMixin:
    """Restores the three named loggers to the state they were in
    before the test: level, handlers and propagate flag.

    Handlers matter as much as levels here. Both configure functions
    add a StreamHandler to stderr, and one left behind on
    'wikiextractor' renders anything a later test propagates up to it
    from 'wikiextractor.extract' -- see this file's own module
    docstring.
    """

    def setUp(self):
        super().setUp()
        self._logger_state = {}
        for name in WIKIEXTRACTOR_LOGGERS:
            logger = logging.getLogger(name)
            self._logger_state[name] = (logger.level, list(logger.handlers),
                                         logger.propagate)

    def tearDown(self):
        for name in WIKIEXTRACTOR_LOGGERS:
            logger = logging.getLogger(name)
            level, handlers, propagate = self._logger_state[name]
            logger.setLevel(level)
            logger.handlers = handlers
            logger.propagate = propagate
        super().tearDown()


class RootLoggerUntouchedTests(NamedLoggerStateMixin, unittest.TestCase):
    """The actual property this architecture exists to guarantee."""

    def setUp(self):
        super().setUp()
        self.root_logger = logging.getLogger()
        self.original_level = self.root_logger.level
        self.original_handlers = list(self.root_logger.handlers)

    def tearDown(self):
        self.root_logger.setLevel(self.original_level)
        self.root_logger.handlers = self.original_handlers
        super().tearDown()

    def test_configure_wikiextractor_logging_does_not_change_root_level(self):
        we.configure_wikiextractor_logging(logging.DEBUG)
        self.assertEqual(self.root_logger.level, self.original_level)

    def test_configure_wikiextractor_logging_does_not_add_root_handlers(self):
        we.configure_wikiextractor_logging(logging.DEBUG)
        self.assertEqual(self.root_logger.handlers, self.original_handlers)

    def test_configure_mapreduce_logging_does_not_touch_root_either(self):
        we.configure_mapreduce_logging(True)
        self.assertEqual(self.root_logger.level, self.original_level)
        self.assertEqual(self.root_logger.handlers, self.original_handlers)


class NamedLoggersIndependentlyConfigurableTests(NamedLoggerStateMixin,
                                                 unittest.TestCase):
    """The actual point of naming them: a programmatic caller can set
    each one's level separately.

    These tests deliberately configure the named loggers; the mixin
    puts level, handlers and propagate back afterward, so later tests
    in the suite see the same unconfigured state they would if this
    file had not run.
    """

    def test_wikiextractor_logger_is_named_correctly(self):
        self.assertEqual(we.wikiextractor_logger.name, 'wikiextractor')

    def test_mapreduce_logger_is_named_correctly(self):
        self.assertEqual(we.mapreduce_logger.name, 'wikiextractor.mapreduce')

    def test_extract_logger_inherits_wikiextractor_level_by_default(self):
        we.configure_wikiextractor_logging(logging.ERROR)
        extract_logger = logging.getLogger('wikiextractor.extract')
        self.assertEqual(extract_logger.getEffectiveLevel(), logging.ERROR)

    def test_extract_logger_level_independently_overridable(self):
        # The actual capability this architecture is meant to enable:
        # a caller wants extraction-mechanics detail without also
        # wanting wikiextractor's own progress/summary lines at the
        # same verbosity, or vice versa.
        we.configure_wikiextractor_logging(logging.WARNING)
        logging.getLogger('wikiextractor.extract').setLevel(logging.DEBUG)
        self.assertEqual(we.wikiextractor_logger.getEffectiveLevel(), logging.WARNING)
        self.assertEqual(logging.getLogger('wikiextractor.extract').getEffectiveLevel(),
                          logging.DEBUG)

    def test_mapreduce_logger_independent_of_wikiextractor_logger(self):
        we.configure_wikiextractor_logging(logging.ERROR)
        we.configure_mapreduce_logging(True)  # -> DEBUG
        self.assertEqual(we.wikiextractor_logger.getEffectiveLevel(), logging.ERROR)
        self.assertEqual(we.mapreduce_logger.getEffectiveLevel(), logging.DEBUG)


if __name__ == '__main__':
    unittest.main()
