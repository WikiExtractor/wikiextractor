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
propagate for every logger in wikiextractor's namespace so nothing
survives the test.

Which loggers those are is discovered from logging's own registry at
the time each test runs, via wikiextractor_loggers() below, rather
than written down here -- a fourth named logger added later is covered
without this file being touched, and a name change to an existing one
cannot leave a stale entry behind that quietly stops being restored.
The namespace root itself comes from wikiextractor_logger.name, so
even renaming that is picked up.

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


def wikiextractor_loggers():
    """Every logger currently registered under wikiextractor's own
    namespace, the top-level one included.

    Read out of logging's registry rather than listed, so this keeps
    working as the namespace grows or its members are renamed. The
    root of the namespace comes from wikiextractor_logger itself, not
    from a literal here.

    Two details of the registry to work around. It also holds
    PlaceHolder objects, standing in for names that only exist as an
    ancestor of some real logger ('wikiextractor.a' when only
    'wikiextractor.a.b' was ever requested); those carry no level,
    handlers or propagate flag, and calling getLogger() on such a name
    would turn it into a real logger, which is itself a state change.
    They are filtered out by type. And the registry can gain entries
    while it is being read, so it is copied to a list first.
    """
    root = we.wikiextractor_logger.name
    prefix = root + '.'
    registry = list(logging.Logger.manager.loggerDict.items())
    return [we.wikiextractor_logger] + [
        logger for name, logger in registry
        if name.startswith(prefix) and isinstance(logger, logging.Logger)
    ]


class NamedLoggerStateMixin:
    """Restores wikiextractor's loggers to the state they were in
    before the test: level, handlers and propagate flag.

    Handlers matter as much as levels here. Both configure functions
    add a StreamHandler to stderr, and one left behind on
    'wikiextractor' renders anything a later test propagates up to it
    from 'wikiextractor.extract' -- see this file's own module
    docstring.
    """

    def setUp(self):
        super().setUp()
        self._logger_state = {
            logger.name: (logger.level, list(logger.handlers), logger.propagate)
            for logger in wikiextractor_loggers()
        }

    def tearDown(self):
        for logger in wikiextractor_loggers():
            saved = self._logger_state.get(logger.name)
            if saved is None:
                # Registered during the test. There is no earlier
                # state to put back, so it gets the state a logger has
                # when it is first created.
                logger.setLevel(logging.NOTSET)
                logger.handlers = []
                logger.propagate = True
                continue
            level, handlers, propagate = saved
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


class LoggerDiscoveryTests(NamedLoggerStateMixin, unittest.TestCase):
    """wikiextractor_loggers() is what keeps this file from going
    stale, so it gets its own coverage."""

    def setUp(self):
        super().setUp()
        self._registered = []

    def tearDown(self):
        # Loggers are never removed from the registry by logging
        # itself, so the ones created here are taken back out --
        # otherwise they stay visible to every later test in the
        # process under a wikiextractor.* name that means nothing.
        for name in self._registered:
            logging.Logger.manager.loggerDict.pop(name, None)
        super().tearDown()

    def register(self, name):
        self._registered.append(name)
        return logging.getLogger(name)

    def names(self):
        return {logger.name for logger in wikiextractor_loggers()}

    def test_the_known_loggers_are_found(self):
        self.assertLessEqual({'wikiextractor', 'wikiextractor.extract',
                              'wikiextractor.mapreduce'},
                             self.names())

    def test_the_namespace_root_itself_is_included(self):
        self.assertIn(we.wikiextractor_logger.name, self.names())

    def test_a_logger_added_later_is_found_without_listing_it(self):
        self.register('wikiextractor.newly_added')
        self.assertIn('wikiextractor.newly_added', self.names())

    def test_a_deeper_child_is_found(self):
        self.register('wikiextractor.extract.templates')
        self.assertIn('wikiextractor.extract.templates', self.names())

    def test_placeholder_ancestors_are_skipped(self):
        # Requesting a.b.c registers PlaceHolder entries for
        # 'wikiextractor.a' and 'wikiextractor.a.b'. Those have no
        # state to save, and asking for them by name would make them
        # real.
        self.register('wikiextractor.a.b.c')
        self._registered.extend(['wikiextractor.a', 'wikiextractor.a.b'])
        found = self.names()
        self.assertIn('wikiextractor.a.b.c', found)
        self.assertNotIn('wikiextractor.a', found)
        self.assertNotIn('wikiextractor.a.b', found)

    def test_loggers_outside_the_namespace_are_left_alone(self):
        self.register('wikiextractorish')  # shares a prefix, not the namespace
        self.register('something.else')
        found = self.names()
        self.assertNotIn('wikiextractorish', found)
        self.assertNotIn('something.else', found)

    def test_everything_returned_is_a_real_logger(self):
        for logger in wikiextractor_loggers():
            with self.subTest(logger=logger):
                self.assertIsInstance(logger, logging.Logger)


class LoggerStateRestorationTests(unittest.TestCase):
    """The mixin's own behavior, exercised through a throwaway test
    case rather than by trusting the other classes here."""

    class Probe(NamedLoggerStateMixin, unittest.TestCase):
        def runTest(self):
            pass

    def run_probe(self, body):
        probe = self.Probe()
        probe.setUp()
        try:
            body()
        finally:
            probe.tearDown()

    def test_a_handler_added_during_the_test_is_removed(self):
        logger = logging.getLogger('wikiextractor')
        before = list(logger.handlers)
        self.run_probe(lambda: we.configure_wikiextractor_logging(logging.DEBUG))
        self.assertEqual(logger.handlers, before)

    def test_a_level_set_during_the_test_is_restored(self):
        logger = logging.getLogger('wikiextractor.extract')
        before = logger.level
        self.run_probe(lambda: logger.setLevel(logging.DEBUG))
        self.assertEqual(logger.level, before)

    def test_propagate_set_during_the_test_is_restored(self):
        logger = logging.getLogger('wikiextractor')
        before = logger.propagate
        self.run_probe(lambda: we.configure_wikiextractor_logging(logging.DEBUG))
        self.assertEqual(logger.propagate, before)

    def test_a_logger_created_during_the_test_is_left_unconfigured(self):
        name = 'wikiextractor.created_midtest'

        def body():
            logger = logging.getLogger(name)
            logger.setLevel(logging.DEBUG)
            logger.addHandler(logging.StreamHandler())
            logger.propagate = False

        try:
            self.run_probe(body)
            logger = logging.getLogger(name)
            self.assertEqual(logger.level, logging.NOTSET)
            self.assertEqual(logger.handlers, [])
            self.assertTrue(logger.propagate)
        finally:
            logging.Logger.manager.loggerDict.pop(name, None)


if __name__ == '__main__':
    unittest.main()
