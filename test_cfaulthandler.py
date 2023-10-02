from contextlib import contextmanager
import datetime
import cfaulthandler
import os
import re
import signal
import subprocess
import sys
from test import support
from test.support import os_helper
from test.support import script_helper, is_android
from test.support import skip_if_sanitizer
import tempfile
import unittest
from textwrap import dedent

try:
    import _testcapi
except ImportError:
    _testcapi = None

if not support.has_subprocess_support:
    raise unittest.SkipTest("test module requires subprocess")

TIMEOUT = 0.5
MS_WINDOWS = (os.name == 'nt')


def expected_traceback(lineno1, lineno2, header, min_count=1):
    regex = header
    regex += '  File "<string>", line %s in func\n' % lineno1
    regex += '  File "<string>", line %s in <module>' % lineno2
    if 1 < min_count:
        return '^' + (regex + '\n') * (min_count - 1) + regex
    else:
        return '^' + regex + '$'

def skip_segfault_on_android(test):
    # Issue #32138: Raising SIGSEGV on Android may not cause a crash.
    return unittest.skipIf(is_android,
                           'raising SIGSEGV on Android is unreliable')(test)

@contextmanager
def temporary_filename():
    filename = tempfile.mktemp()
    try:
        yield filename
    finally:
        os_helper.unlink(filename)

class CFaultHandlerTests(unittest.TestCase):

    def get_output(self, code, filename=None, fd=None):
        """
        Run the specified code in Python (in a new child process) and read the
        output from the standard error or from a file (if filename is set).
        Return the output lines as a list.

        Strip the reference count from the standard error for Python debug
        build, and replace "Current thread 0x00007f8d8fbd9700" by "Current
        thread XXX".
        """
        code = dedent(code).strip()
        pass_fds = []
        if fd is not None:
            pass_fds.append(fd)
        with support.SuppressCrashReport():
            process = script_helper.spawn_python('-c', code, pass_fds=pass_fds)
            with process:
                output, stderr = process.communicate()
                exitcode = process.wait()
        output = output.decode('ascii', 'backslashreplace')
        if filename:
            self.assertEqual(output, '')
            with open(filename, "rb") as fp:
                output = fp.read()
            output = output.decode('ascii', 'backslashreplace')
        elif fd is not None:
            self.assertEqual(output, '')
            os.lseek(fd, os.SEEK_SET, 0)
            with open(fd, "rb", closefd=False) as fp:
                output = fp.read()
            output = output.decode('ascii', 'backslashreplace')
        return output.splitlines(), exitcode

    def check_error(self, code, lineno, fatal_error, *,
                    filename=None, all_threads=True, other_regex=None,
                    fd=None, know_current_thread=True,
                    py_fatal_error=False,
                    garbage_collecting=False,
                    function='<module>'):
        """
        Check that the fault handler for fatal errors is enabled and check the
        traceback from the child process output.

        Raise an error if the output doesn't match the expected format.
        """
        if all_threads:
            if know_current_thread:
                header = 'Current thread 0x[0-9a-f]+'
            else:
                header = 'Thread 0x[0-9a-f]+'
        else:
            header = 'Stack'
        regex = [f'^{fatal_error}']
        if py_fatal_error:
            regex.append("Python runtime state: initialized")
        regex.append('')
        regex.append(fr'{header} \(most recent call first\):')
        if garbage_collecting:
            regex.append('  Garbage-collecting')
        regex.append(fr'  File "<string>", line {lineno} in {function}')
        regex = '\n'.join(regex)

        if other_regex:
            regex = f'(?:{regex}|{other_regex})'

        # Enable MULTILINE flag
        regex = f'(?m){regex}'
        output, exitcode = self.get_output(code, filename=filename, fd=fd)
        output = '\n'.join(output)
        self.assertRegex(output, regex)
        self.assertNotEqual(exitcode, 0)

    def check_fatal_error(self, code, line_number, name_regex, func=None, **kw):
        if func:
            name_regex = '%s: %s' % (func, name_regex)
        fatal_error = 'Fatal Python error: %s' % name_regex
        self.check_error(code, line_number, fatal_error, **kw)

    def check_windows_exception(self, code, line_number, name_regex, **kw):
        fatal_error = 'Windows fatal exception: %s' % name_regex
        self.check_error(code, line_number, fatal_error, **kw)

    @unittest.skipIf(sys.platform.startswith('aix'),
                     "the first page of memory is a mapped read-only on AIX")
    def test_read_null(self):
        if not MS_WINDOWS:
            self.check_fatal_error("""
                import cfaulthandler
                cfaulthandler.enable()
                cfaulthandler._read_null()
                """,
                3,
                # Issue #12700: Read NULL raises SIGILL on Mac OS X Lion
                '(?:Segmentation fault'
                    '|Bus error'
                    '|Illegal instruction)')
        else:
            self.check_windows_exception("""
                import cfaulthandler
                cfaulthandler.enable()
                cfaulthandler._read_null()
                """,
                3,
                'access violation')

    @skip_segfault_on_android
    def test_sigsegv(self):
        self.check_fatal_error("""
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler._sigsegv()
            """,
            3,
            'Segmentation fault')

    @skip_segfault_on_android
    def test_gc(self):
        # bpo-44466: Detect if the GC is running
        self.check_fatal_error("""
            import cfaulthandler
            import gc
            import sys

            cfaulthandler.enable()

            class RefCycle:
                def __del__(self):
                    cfaulthandler._sigsegv()

            # create a reference cycle which triggers a fatal
            # error in a destructor
            a = RefCycle()
            b = RefCycle()
            a.b = b
            b.a = a

            # Delete the objects, not the cycle
            a = None
            b = None

            # Break the reference cycle: call __del__()
            gc.collect()

            # Should not reach this line
            print("exit", file=sys.stderr)
            """,
            9,
            'Segmentation fault',
            function='__del__',
            garbage_collecting=True)

    def test_fatal_error_c_thread(self):
        self.check_fatal_error("""
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler._fatal_error_c_thread()
            """,
            3,
            'in new thread',
            know_current_thread=False,
            func='cfaulthandler_fatal_error_thread',
            py_fatal_error=True)

    def test_sigabrt(self):
        self.check_fatal_error("""
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler._sigabrt()
            """,
            3,
            'Aborted')

    @unittest.skipIf(sys.platform == 'win32',
                     "SIGFPE cannot be caught on Windows")
    def test_sigfpe(self):
        self.check_fatal_error("""
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler._sigfpe()
            """,
            3,
            'Floating point exception')

    @unittest.skipIf(_testcapi is None, 'need _testcapi')
    @unittest.skipUnless(hasattr(signal, 'SIGBUS'), 'need signal.SIGBUS')
    @skip_segfault_on_android
    def test_sigbus(self):
        self.check_fatal_error("""
            import cfaulthandler
            import signal

            cfaulthandler.enable()
            signal.raise_signal(signal.SIGBUS)
            """,
            5,
            'Bus error')

    @unittest.skipIf(_testcapi is None, 'need _testcapi')
    @unittest.skipUnless(hasattr(signal, 'SIGILL'), 'need signal.SIGILL')
    @skip_segfault_on_android
    def test_sigill(self):
        self.check_fatal_error("""
            import cfaulthandler
            import signal

            cfaulthandler.enable()
            signal.raise_signal(signal.SIGILL)
            """,
            5,
            'Illegal instruction')

    def check_fatal_error_func(self, release_gil):
        # Test that Py_FatalError() dumps a traceback
        with support.SuppressCrashReport():
            self.check_fatal_error(f"""
                import _testcapi
                _testcapi.fatal_error(b'xyz', {release_gil})
                """,
                2,
                'xyz',
                func='test_fatal_error',
                py_fatal_error=True)

    def test_fatal_error(self):
        self.check_fatal_error_func(False)

    def test_fatal_error_without_gil(self):
        self.check_fatal_error_func(True)

    @unittest.skipIf(sys.platform.startswith('openbsd'),
                     "Issue #12868: sigaltstack() doesn't work on "
                     "OpenBSD if Python is compiled with pthread")
    @unittest.skipIf(not hasattr(cfaulthandler, '_stack_overflow'),
                     'need cfaulthandler._stack_overflow()')
    def test_stack_overflow(self):
        self.check_fatal_error("""
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler._stack_overflow()
            """,
            3,
            '(?:Segmentation fault|Bus error)',
            other_regex='unable to raise a stack overflow')

    @skip_segfault_on_android
    def test_gil_released(self):
        self.check_fatal_error("""
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler._sigsegv(True)
            """,
            3,
            'Segmentation fault')

    @skip_if_sanitizer(memory=True, ub=True, reason="sanitizer "
                       "builds change crashing process output.")
    @skip_segfault_on_android
    def test_enable_file(self):
        with temporary_filename() as filename:
            self.check_fatal_error("""
                import cfaulthandler
                output = open({filename}, 'wb')
                cfaulthandler.enable(output)
                cfaulthandler._sigsegv()
                """.format(filename=repr(filename)),
                4,
                'Segmentation fault',
                filename=filename)

    @unittest.skipIf(sys.platform == "win32",
                     "subprocess doesn't support pass_fds on Windows")
    @skip_if_sanitizer(memory=True, ub=True, reason="sanitizer "
                       "builds change crashing process output.")
    @skip_segfault_on_android
    def test_enable_fd(self):
        with tempfile.TemporaryFile('wb+') as fp:
            fd = fp.fileno()
            self.check_fatal_error("""
                import cfaulthandler
                import sys
                cfaulthandler.enable(%s)
                cfaulthandler._sigsegv()
                """ % fd,
                4,
                'Segmentation fault',
                fd=fd)

    @skip_segfault_on_android
    def test_enable_single_thread(self):
        self.check_fatal_error("""
            import cfaulthandler
            cfaulthandler.enable(all_threads=False)
            cfaulthandler._sigsegv()
            """,
            3,
            'Segmentation fault',
            all_threads=False)

    @skip_segfault_on_android
    def test_disable(self):
        code = """
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler.disable()
            cfaulthandler._sigsegv()
            """
        not_expected = 'Fatal Python error'
        stderr, exitcode = self.get_output(code)
        stderr = '\n'.join(stderr)
        self.assertTrue(not_expected not in stderr,
                     "%r is present in %r" % (not_expected, stderr))
        self.assertNotEqual(exitcode, 0)

    @skip_segfault_on_android
    def test_dump_ext_modules(self):
        code = """
            import cfaulthandler
            import sys
            # Don't filter stdlib module names
            sys.stdlib_module_names = frozenset()
            cfaulthandler.enable()
            cfaulthandler._sigsegv()
            """
        stderr, exitcode = self.get_output(code)
        stderr = '\n'.join(stderr)
        match = re.search(r'^Extension modules:(.*) \(total: [0-9]+\)$',
                          stderr, re.MULTILINE)
        if not match:
            self.fail(f"Cannot find 'Extension modules:' in {stderr!r}")
        modules = set(match.group(1).strip().split(', '))
        for name in ('sys', 'cfaulthandler'):
            self.assertIn(name, modules)

    def test_is_enabled(self):
        orig_stderr = sys.stderr
        try:
            # regrtest may replace sys.stderr by io.StringIO object, but
            # cfaulthandler.enable() requires that sys.stderr has a fileno()
            # method
            sys.stderr = sys.__stderr__

            was_enabled = cfaulthandler.is_enabled()
            try:
                cfaulthandler.enable()
                self.assertTrue(cfaulthandler.is_enabled())
                cfaulthandler.disable()
                self.assertFalse(cfaulthandler.is_enabled())
            finally:
                if was_enabled:
                    cfaulthandler.enable()
                else:
                    cfaulthandler.disable()
        finally:
            sys.stderr = orig_stderr

    @support.requires_subprocess()
    def test_disabled_by_default(self):
        # By default, the module should be disabled
        code = "import cfaulthandler; print(cfaulthandler.is_enabled())"
        args = (sys.executable, "-E", "-c", code)
        # don't use assert_python_ok() because it always enables cfaulthandler
        output = subprocess.check_output(args)
        self.assertEqual(output.rstrip(), b"False")

    @support.requires_subprocess()
    def test_sys_xoptions(self):
        # Test python -X cfaulthandler
        code = "import cfaulthandler; print(cfaulthandler.is_enabled())"
        args = filter(None, (sys.executable,
                             "-E" if sys.flags.ignore_environment else "",
                             "-X", "cfaulthandler", "-c", code))
        env = os.environ.copy()
        env.pop("PYTHONFAULTHANDLER", None)
        # don't use assert_python_ok() because it always enables cfaulthandler
        output = subprocess.check_output(args, env=env)
        self.assertEqual(output.rstrip(), b"True")

    @support.requires_subprocess()
    def test_env_var(self):
        # empty env var
        code = "import cfaulthandler; print(cfaulthandler.is_enabled())"
        args = (sys.executable, "-c", code)
        env = dict(os.environ)
        env['PYTHONFAULTHANDLER'] = ''
        env['PYTHONDEVMODE'] = ''
        # don't use assert_python_ok() because it always enables cfaulthandler
        output = subprocess.check_output(args, env=env)
        self.assertEqual(output.rstrip(), b"False")

        # non-empty env var
        env = dict(os.environ)
        env['PYTHONFAULTHANDLER'] = '1'
        env['PYTHONDEVMODE'] = ''
        output = subprocess.check_output(args, env=env)
        self.assertEqual(output.rstrip(), b"True")

    def check_dump_traceback(self, *, filename=None, fd=None):
        """
        Explicitly call dump_traceback() function and check its output.
        Raise an error if the output doesn't match the expected format.
        """
        code = """
            import cfaulthandler

            filename = {filename!r}
            fd = {fd}

            def funcB():
                if filename:
                    with open(filename, "wb") as fp:
                        cfaulthandler.dump_traceback(fp, all_threads=False)
                elif fd is not None:
                    cfaulthandler.dump_traceback(fd,
                                                all_threads=False)
                else:
                    cfaulthandler.dump_traceback(all_threads=False)

            def funcA():
                funcB()

            funcA()
            """
        code = code.format(
            filename=filename,
            fd=fd,
        )
        if filename:
            lineno = 9
        elif fd is not None:
            lineno = 11
        else:
            lineno = 14
        expected = [
            'Stack (most recent call first):',
            '  File "<string>", line %s in funcB' % lineno,
            '  File "<string>", line 17 in funcA',
            '  File "<string>", line 19 in <module>'
        ]
        trace, exitcode = self.get_output(code, filename, fd)
        self.assertEqual(trace, expected)
        self.assertEqual(exitcode, 0)

    def test_dump_traceback(self):
        self.check_dump_traceback()

    def test_dump_traceback_file(self):
        with temporary_filename() as filename:
            self.check_dump_traceback(filename=filename)

    @unittest.skipIf(sys.platform == "win32",
                     "subprocess doesn't support pass_fds on Windows")
    def test_dump_traceback_fd(self):
        with tempfile.TemporaryFile('wb+') as fp:
            self.check_dump_traceback(fd=fp.fileno())

    def test_truncate(self):
        maxlen = 500
        func_name = 'x' * (maxlen + 50)
        truncated = 'x' * maxlen + '...'
        code = """
            import cfaulthandler

            def {func_name}():
                cfaulthandler.dump_traceback(all_threads=False)

            {func_name}()
            """
        code = code.format(
            func_name=func_name,
        )
        expected = [
            'Stack (most recent call first):',
            '  File "<string>", line 4 in %s' % truncated,
            '  File "<string>", line 6 in <module>'
        ]
        trace, exitcode = self.get_output(code)
        self.assertEqual(trace, expected)
        self.assertEqual(exitcode, 0)

    def check_dump_traceback_threads(self, filename):
        """
        Call explicitly dump_traceback(all_threads=True) and check the output.
        Raise an error if the output doesn't match the expected format.
        """
        code = """
            import cfaulthandler
            from threading import Thread, Event
            import time

            def dump():
                if {filename}:
                    with open({filename}, "wb") as fp:
                        cfaulthandler.dump_traceback(fp, all_threads=True)
                else:
                    cfaulthandler.dump_traceback(all_threads=True)

            class Waiter(Thread):
                # avoid blocking if the main thread raises an exception.
                daemon = True

                def __init__(self):
                    Thread.__init__(self)
                    self.running = Event()
                    self.stop = Event()

                def run(self):
                    self.running.set()
                    self.stop.wait()

            waiter = Waiter()
            waiter.start()
            waiter.running.wait()
            dump()
            waiter.stop.set()
            waiter.join()
            """
        code = code.format(filename=repr(filename))
        output, exitcode = self.get_output(code, filename)
        output = '\n'.join(output)
        if filename:
            lineno = 8
        else:
            lineno = 10
        regex = r"""
            ^Thread 0x[0-9a-f]+ \(most recent call first\):
            (?:  File ".*threading.py", line [0-9]+ in [_a-z]+
            ){{1,3}}  File "<string>", line 23 in run
              File ".*threading.py", line [0-9]+ in _bootstrap_inner
              File ".*threading.py", line [0-9]+ in _bootstrap

            Current thread 0x[0-9a-f]+ \(most recent call first\):
              File "<string>", line {lineno} in dump
              File "<string>", line 28 in <module>$
            """
        regex = dedent(regex.format(lineno=lineno)).strip()
        self.assertRegex(output, regex)
        self.assertEqual(exitcode, 0)

    def test_dump_traceback_threads(self):
        self.check_dump_traceback_threads(None)

    def test_dump_traceback_threads_file(self):
        with temporary_filename() as filename:
            self.check_dump_traceback_threads(filename)

    def check_dump_traceback_later(self, repeat=False, cancel=False, loops=1,
                                   *, filename=None, fd=None):
        """
        Check how many times the traceback is written in timeout x 2.5 seconds,
        or timeout x 3.5 seconds if cancel is True: 1, 2 or 3 times depending
        on repeat and cancel options.

        Raise an error if the output doesn't match the expect format.
        """
        timeout_str = str(datetime.timedelta(seconds=TIMEOUT))
        code = """
            import cfaulthandler
            import time
            import sys

            timeout = {timeout}
            repeat = {repeat}
            cancel = {cancel}
            loops = {loops}
            filename = {filename!r}
            fd = {fd}

            def func(timeout, repeat, cancel, file, loops):
                for loop in range(loops):
                    cfaulthandler.dump_traceback_later(timeout, repeat=repeat, file=file)
                    if cancel:
                        cfaulthandler.cancel_dump_traceback_later()
                    time.sleep(timeout * 5)
                    cfaulthandler.cancel_dump_traceback_later()

            if filename:
                file = open(filename, "wb")
            elif fd is not None:
                file = sys.stderr.fileno()
            else:
                file = None
            func(timeout, repeat, cancel, file, loops)
            if filename:
                file.close()
            """
        code = code.format(
            timeout=TIMEOUT,
            repeat=repeat,
            cancel=cancel,
            loops=loops,
            filename=filename,
            fd=fd,
        )
        trace, exitcode = self.get_output(code, filename)
        trace = '\n'.join(trace)

        if not cancel:
            count = loops
            if repeat:
                count *= 2
            header = r'Timeout \(%s\)!\nThread 0x[0-9a-f]+ \(most recent call first\):\n' % timeout_str
            regex = expected_traceback(17, 26, header, min_count=count)
            self.assertRegex(trace, regex)
        else:
            self.assertEqual(trace, '')
        self.assertEqual(exitcode, 0)

    def test_dump_traceback_later(self):
        self.check_dump_traceback_later()

    def test_dump_traceback_later_repeat(self):
        self.check_dump_traceback_later(repeat=True)

    def test_dump_traceback_later_cancel(self):
        self.check_dump_traceback_later(cancel=True)

    def test_dump_traceback_later_file(self):
        with temporary_filename() as filename:
            self.check_dump_traceback_later(filename=filename)

    @unittest.skipIf(sys.platform == "win32",
                     "subprocess doesn't support pass_fds on Windows")
    def test_dump_traceback_later_fd(self):
        with tempfile.TemporaryFile('wb+') as fp:
            self.check_dump_traceback_later(fd=fp.fileno())

    def test_dump_traceback_later_twice(self):
        self.check_dump_traceback_later(loops=2)

    @unittest.skipIf(not hasattr(cfaulthandler, "register"),
                     "need cfaulthandler.register")
    def check_register(self, filename=False, all_threads=False,
                       unregister=False, chain=False, fd=None):
        """
        Register a handler displaying the traceback on a user signal. Raise the
        signal and check the written traceback.

        If chain is True, check that the previous signal handler is called.

        Raise an error if the output doesn't match the expected format.
        """
        signum = signal.SIGUSR1
        code = """
            import cfaulthandler
            import os
            import signal
            import sys

            all_threads = {all_threads}
            signum = {signum:d}
            unregister = {unregister}
            chain = {chain}
            filename = {filename!r}
            fd = {fd}

            def func(signum):
                os.kill(os.getpid(), signum)

            def handler(signum, frame):
                handler.called = True
            handler.called = False

            if filename:
                file = open(filename, "wb")
            elif fd is not None:
                file = sys.stderr.fileno()
            else:
                file = None
            if chain:
                signal.signal(signum, handler)
            cfaulthandler.register(signum, file=file,
                                  all_threads=all_threads, chain={chain})
            if unregister:
                cfaulthandler.unregister(signum)
            func(signum)
            if chain and not handler.called:
                if file is not None:
                    output = file
                else:
                    output = sys.stderr
                print("Error: signal handler not called!", file=output)
                exitcode = 1
            else:
                exitcode = 0
            if filename:
                file.close()
            sys.exit(exitcode)
            """
        code = code.format(
            all_threads=all_threads,
            signum=signum,
            unregister=unregister,
            chain=chain,
            filename=filename,
            fd=fd,
        )
        trace, exitcode = self.get_output(code, filename)
        trace = '\n'.join(trace)
        if not unregister:
            if all_threads:
                regex = r'Current thread 0x[0-9a-f]+ \(most recent call first\):\n'
            else:
                regex = r'Stack \(most recent call first\):\n'
            regex = expected_traceback(14, 32, regex)
            self.assertRegex(trace, regex)
        else:
            self.assertEqual(trace, '')
        if unregister:
            self.assertNotEqual(exitcode, 0)
        else:
            self.assertEqual(exitcode, 0)

    def test_register(self):
        self.check_register()

    def test_unregister(self):
        self.check_register(unregister=True)

    def test_register_file(self):
        with temporary_filename() as filename:
            self.check_register(filename=filename)

    @unittest.skipIf(sys.platform == "win32",
                     "subprocess doesn't support pass_fds on Windows")
    def test_register_fd(self):
        with tempfile.TemporaryFile('wb+') as fp:
            self.check_register(fd=fp.fileno())

    def test_register_threads(self):
        self.check_register(all_threads=True)

    def test_register_chain(self):
        self.check_register(chain=True)

    @contextmanager
    def check_stderr_none(self):
        stderr = sys.stderr
        try:
            sys.stderr = None
            with self.assertRaises(RuntimeError) as cm:
                yield
            self.assertEqual(str(cm.exception), "sys.stderr is None")
        finally:
            sys.stderr = stderr

    def test_stderr_None(self):
        # Issue #21497: provide a helpful error if sys.stderr is None,
        # instead of just an attribute error: "None has no attribute fileno".
        with self.check_stderr_none():
            cfaulthandler.enable()
        with self.check_stderr_none():
            cfaulthandler.dump_traceback()
        with self.check_stderr_none():
            cfaulthandler.dump_traceback_later(1e-3)
        if hasattr(cfaulthandler, "register"):
            with self.check_stderr_none():
                cfaulthandler.register(signal.SIGUSR1)

    @unittest.skipUnless(MS_WINDOWS, 'specific to Windows')
    def test_raise_exception(self):
        for exc, name in (
            ('EXCEPTION_ACCESS_VIOLATION', 'access violation'),
            ('EXCEPTION_INT_DIVIDE_BY_ZERO', 'int divide by zero'),
            ('EXCEPTION_STACK_OVERFLOW', 'stack overflow'),
        ):
            self.check_windows_exception(f"""
                import cfaulthandler
                cfaulthandler.enable()
                cfaulthandler._raise_exception(cfaulthandler._{exc})
                """,
                3,
                name)

    @unittest.skipUnless(MS_WINDOWS, 'specific to Windows')
    def test_ignore_exception(self):
        for exc_code in (
            0xE06D7363,   # MSC exception ("Emsc")
            0xE0434352,   # COM Callable Runtime exception ("ECCR")
        ):
            code = f"""
                    import cfaulthandler
                    cfaulthandler.enable()
                    cfaulthandler._raise_exception({exc_code})
                    """
            code = dedent(code)
            output, exitcode = self.get_output(code)
            self.assertEqual(output, [])
            self.assertEqual(exitcode, exc_code)

    @unittest.skipUnless(MS_WINDOWS, 'specific to Windows')
    def test_raise_nonfatal_exception(self):
        # These exceptions are not strictly errors. Letting
        # cfaulthandler display the traceback when they are
        # raised is likely to result in noise. However, they
        # may still terminate the process if there is no
        # handler installed for them (which there typically
        # is, e.g. for debug messages).
        for exc in (
            0x00000000,
            0x34567890,
            0x40000000,
            0x40001000,
            0x70000000,
            0x7FFFFFFF,
        ):
            output, exitcode = self.get_output(f"""
                import cfaulthandler
                cfaulthandler.enable()
                cfaulthandler._raise_exception(0x{exc:x})
                """
            )
            self.assertEqual(output, [])
            # On Windows older than 7 SP1, the actual exception code has
            # bit 29 cleared.
            self.assertIn(exitcode,
                          (exc, exc & ~0x10000000))

    @unittest.skipUnless(MS_WINDOWS, 'specific to Windows')
    def test_disable_windows_exc_handler(self):
        code = dedent("""
            import cfaulthandler
            cfaulthandler.enable()
            cfaulthandler.disable()
            code = cfaulthandler._EXCEPTION_ACCESS_VIOLATION
            cfaulthandler._raise_exception(code)
        """)
        output, exitcode = self.get_output(code)
        self.assertEqual(output, [])
        self.assertEqual(exitcode, 0xC0000005)

    def test_cancel_later_without_dump_traceback_later(self):
        # bpo-37933: Calling cancel_dump_traceback_later()
        # without dump_traceback_later() must not segfault.
        code = dedent("""
            import cfaulthandler
            cfaulthandler.cancel_dump_traceback_later()
        """)
        output, exitcode = self.get_output(code)
        self.assertEqual(output, [])
        self.assertEqual(exitcode, 0)


if __name__ == "__main__":
    unittest.main()