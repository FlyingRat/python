import unittest
from test import support
from contextlib import closing
import gc
import pickle
import select
import signal
import struct
import subprocess
import traceback
import sys, os, time, errno
from test.script_helper import assert_python_ok, spawn_python
try:
    import threading
except ImportError:
    threading = None

if sys.platform in ('os2', 'riscos'):
    raise unittest.SkipTest("Can't test signal on %s" % sys.platform)


class HandlerBCalled(Exception):
    pass


def exit_subprocess():
    """Use os._exit(0) to exit the current subprocess.

    Otherwise, the test catches the SystemExit and continues executing
    in parallel with the original test, so you wind up with an
    exponential number of tests running concurrently.
    """
    os._exit(0)


def ignoring_eintr(__func, *args, **kwargs):
    try:
        return __func(*args, **kwargs)
    except EnvironmentError as e:
        if e.errno != errno.EINTR:
            raise
        return None


@unittest.skipIf(sys.platform == "win32", "Not valid on Windows")
class InterProcessSignalTests(unittest.TestCase):
    MAX_DURATION = 20   # Entire test should last at most 20 sec.

    def setUp(self):
        self.using_gc = gc.isenabled()
        gc.disable()

    def tearDown(self):
        if self.using_gc:
            gc.enable()

    def format_frame(self, frame, limit=None):
        return ''.join(traceback.format_stack(frame, limit=limit))

    def handlerA(self, signum, frame):
        self.a_called = True

    def handlerB(self, signum, frame):
        self.b_called = True
        raise HandlerBCalled(signum, self.format_frame(frame))

    def wait(self, child):
        """Wait for child to finish, ignoring EINTR."""
        while True:
            try:
                child.wait()
                return
            except OSError as e:
                if e.errno != errno.EINTR:
                    raise

    def run_test(self):
        # Install handlers. This function runs in a sub-process, so we
        # don't worry about re-setting the default handlers.
        signal.signal(signal.SIGHUP, self.handlerA)
        signal.signal(signal.SIGUSR1, self.handlerB)
        signal.signal(signal.SIGUSR2, signal.SIG_IGN)
        signal.signal(signal.SIGALRM, signal.default_int_handler)

        # Variables the signals will modify:
        self.a_called = False
        self.b_called = False

        # Let the sub-processes know who to send signals to.
        pid = os.getpid()

        child = ignoring_eintr(subprocess.Popen, ['kill', '-HUP', str(pid)])
        if child:
            self.wait(child)
            if not self.a_called:
                time.sleep(1)  # Give the signal time to be delivered.
        self.assertTrue(self.a_called)
        self.assertFalse(self.b_called)
        self.a_called = False

        # Make sure the signal isn't delivered while the previous
        # Popen object is being destroyed, because __del__ swallows
        # exceptions.
        del child
        try:
            child = subprocess.Popen(['kill', '-USR1', str(pid)])
            # This wait should be interrupted by the signal's exception.
            self.wait(child)
            time.sleep(1)  # Give the signal time to be delivered.
            self.fail('HandlerBCalled exception not thrown')
        except HandlerBCalled:
            self.assertTrue(self.b_called)
            self.assertFalse(self.a_called)

        child = ignoring_eintr(subprocess.Popen, ['kill', '-USR2', str(pid)])
        if child:
            self.wait(child)  # Nothing should happen.

        try:
            signal.alarm(1)
            # The race condition in pause doesn't matter in this case,
            # since alarm is going to raise a KeyboardException, which
            # will skip the call.
            signal.pause()
            # But if another signal arrives before the alarm, pause
            # may return early.
            time.sleep(1)
        except KeyboardInterrupt:
            pass
        except:
            self.fail("Some other exception woke us from pause: %s" %
                      traceback.format_exc())
        else:
            self.fail("pause returned of its own accord, and the signal"
                      " didn't arrive after another second.")

    # Issue 3864, unknown if this affects earlier versions of freebsd also
    @unittest.skipIf(sys.platform=='freebsd6',
        'inter process signals not reliable (do not mix well with threading) '
        'on freebsd6')
    def test_main(self):
        # This function spawns a child process to insulate the main
        # test-running process from all the signals. It then
        # communicates with that child process over a pipe and
        # re-raises information about any exceptions the child
        # throws. The real work happens in self.run_test().
        os_done_r, os_done_w = os.pipe()
        with closing(os.fdopen(os_done_r, 'rb')) as done_r, \
             closing(os.fdopen(os_done_w, 'wb')) as done_w:
            child = os.fork()
            if child == 0:
                # In the child process; run the test and report results
                # through the pipe.
                try:
                    done_r.close()
                    # Have to close done_w again here because
                    # exit_subprocess() will skip the enclosing with block.
                    with closing(done_w):
                        try:
                            self.run_test()
                        except:
                            pickle.dump(traceback.format_exc(), done_w)
                        else:
                            pickle.dump(None, done_w)
                except:
                    print('Uh oh, raised from pickle.')
                    traceback.print_exc()
                finally:
                    exit_subprocess()

            done_w.close()
            # Block for up to MAX_DURATION seconds for the test to finish.
            r, w, x = select.select([done_r], [], [], self.MAX_DURATION)
            if done_r in r:
                tb = pickle.load(done_r)
                if tb:
                    self.fail(tb)
            else:
                os.kill(child, signal.SIGKILL)
                self.fail('Test deadlocked after %d seconds.' %
                          self.MAX_DURATION)


@unittest.skipIf(sys.platform == "win32", "Not valid on Windows")
class PosixTests(unittest.TestCase):
    def trivial_signal_handler(self, *args):
        pass

    def test_out_of_range_signal_number_raises_error(self):
        self.assertRaises(ValueError, signal.getsignal, 4242)

        self.assertRaises(ValueError, signal.signal, 4242,
                          self.trivial_signal_handler)

    def test_setting_signal_handler_to_none_raises_error(self):
        self.assertRaises(TypeError, signal.signal,
                          signal.SIGUSR1, None)

    def test_getsignal(self):
        hup = signal.signal(signal.SIGHUP, self.trivial_signal_handler)
        self.assertEqual(signal.getsignal(signal.SIGHUP),
                         self.trivial_signal_handler)
        signal.signal(signal.SIGHUP, hup)
        self.assertEqual(signal.getsignal(signal.SIGHUP), hup)


@unittest.skipUnless(sys.platform == "win32", "Windows specific")
class WindowsSignalTests(unittest.TestCase):
    def test_issue9324(self):
        # Updated for issue #10003, adding SIGBREAK
        handler = lambda x, y: None
        for sig in (signal.SIGABRT, signal.SIGBREAK, signal.SIGFPE,
                    signal.SIGILL, signal.SIGINT, signal.SIGSEGV,
                    signal.SIGTERM):
            # Set and then reset a handler for signals that work on windows
            signal.signal(sig, signal.signal(sig, handler))

        with self.assertRaises(ValueError):
            signal.signal(-1, handler)

        with self.assertRaises(ValueError):
            signal.signal(7, handler)


@unittest.skipIf(sys.platform == "win32", "Not valid on Windows")
class WakeupSignalTests(unittest.TestCase):
    TIMEOUT_FULL = 10
    TIMEOUT_HALF = 5

    def handler(self, signum, frame):
        pass

    def check_signum(self, *signals):
        data = os.read(self.read, len(signals)+1)
        raised = struct.unpack('%uB' % len(data), data)
        # We don't care of the signal delivery order (it's not portable or
        # reliable)
        raised = set(raised)
        signals = set(signals)
        self.assertEqual(raised, signals)

    def test_wakeup_fd_early(self):
        import select

        signal.alarm(1)
        before_time = time.time()
        # We attempt to get a signal during the sleep,
        # before select is called
        time.sleep(self.TIMEOUT_FULL)
        mid_time = time.time()
        self.assertTrue(mid_time - before_time < self.TIMEOUT_HALF)
        select.select([self.read], [], [], self.TIMEOUT_FULL)
        after_time = time.time()
        self.assertTrue(after_time - mid_time < self.TIMEOUT_HALF)
        self.check_signum(signal.SIGALRM)

    def test_wakeup_fd_during(self):
        import select

        signal.alarm(1)
        before_time = time.time()
        # We attempt to get a signal during the select call
        self.assertRaises(select.error, select.select,
            [self.read], [], [], self.TIMEOUT_FULL)
        after_time = time.time()
        self.assertTrue(after_time - before_time < self.TIMEOUT_HALF)
        self.check_signum(signal.SIGALRM)

    def test_signum(self):
        old_handler = signal.signal(signal.SIGUSR1, self.handler)
        self.addCleanup(signal.signal, signal.SIGUSR1, old_handler)
        os.kill(os.getpid(), signal.SIGUSR1)
        os.kill(os.getpid(), signal.SIGALRM)
        self.check_signum(signal.SIGUSR1, signal.SIGALRM)

    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'),
                         'need signal.pthread_sigmask()')
    @unittest.skipUnless(hasattr(signal, 'pthread_kill'),
                         'need signal.pthread_kill()')
    def test_pending(self):
        signum1 = signal.SIGUSR1
        signum2 = signal.SIGUSR2
        tid = threading.current_thread().ident

        old_handler = signal.signal(signum1, self.handler)
        self.addCleanup(signal.signal, signum1, old_handler)
        old_handler = signal.signal(signum2, self.handler)
        self.addCleanup(signal.signal, signum2, old_handler)

        signal.pthread_sigmask(signal.SIG_BLOCK, (signum1, signum2))
        signal.pthread_kill(tid, signum1)
        signal.pthread_kill(tid, signum2)
        # Unblocking the 2 signals calls the C signal handler twice
        signal.pthread_sigmask(signal.SIG_UNBLOCK, (signum1, signum2))

        self.check_signum(signum1, signum2)

    def setUp(self):
        import fcntl

        self.alrm = signal.signal(signal.SIGALRM, self.handler)
        self.read, self.write = os.pipe()
        flags = fcntl.fcntl(self.write, fcntl.F_GETFL, 0)
        flags = flags | os.O_NONBLOCK
        fcntl.fcntl(self.write, fcntl.F_SETFL, flags)
        self.old_wakeup = signal.set_wakeup_fd(self.write)

    def tearDown(self):
        signal.set_wakeup_fd(self.old_wakeup)
        os.close(self.read)
        os.close(self.write)
        signal.signal(signal.SIGALRM, self.alrm)

@unittest.skipIf(sys.platform == "win32", "Not valid on Windows")
class SiginterruptTest(unittest.TestCase):

    def readpipe_interrupted(self, interrupt):
        """Perform a read during which a signal will arrive.  Return True if the
        read is interrupted by the signal and raises an exception.  Return False
        if it returns normally.
        """
        # use a subprocess to have only one thread, to have a timeout on the
        # blocking read and to not touch signal handling in this process
        code = """if 1:
            import errno
            import os
            import signal
            import sys

            interrupt = %r
            r, w = os.pipe()

            def handler(signum, frame):
                pass

            print("ready")
            sys.stdout.flush()

            signal.signal(signal.SIGALRM, handler)
            if interrupt is not None:
                signal.siginterrupt(signal.SIGALRM, interrupt)

            # run the test twice
            for loop in range(2):
                # send a SIGALRM in a second (during the read)
                signal.alarm(1)
                try:
                    # blocking call: read from a pipe without data
                    os.read(r, 1)
                except OSError as err:
                    if err.errno != errno.EINTR:
                        raise
                else:
                    sys.exit(2)
            sys.exit(3)
        """ % (interrupt,)
        with spawn_python('-c', code) as process:
            try:
                # wait until the child process is loaded and has started
                first_line = process.stdout.readline()

                stdout, stderr = process.communicate(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                return False
            else:
                stdout = first_line + stdout
                exitcode = process.wait()
                if exitcode not in (2, 3):
                    raise Exception("Child error (exit code %s): %s"
                                    % (exitcode, stdout))
                return (exitcode == 3)

    def test_without_siginterrupt(self):
        # If a signal handler is installed and siginterrupt is not called
        # at all, when that signal arrives, it interrupts a syscall that's in
        # progress.
        interrupted = self.readpipe_interrupted(None)
        self.assertTrue(interrupted)

    def test_siginterrupt_on(self):
        # If a signal handler is installed and siginterrupt is called with
        # a true value for the second argument, when that signal arrives, it
        # interrupts a syscall that's in progress.
        interrupted = self.readpipe_interrupted(True)
        self.assertTrue(interrupted)

    def test_siginterrupt_off(self):
        # If a signal handler is installed and siginterrupt is called with
        # a false value for the second argument, when that signal arrives, it
        # does not interrupt a syscall that's in progress.
        interrupted = self.readpipe_interrupted(False)
        self.assertFalse(interrupted)


@unittest.skipIf(sys.platform == "win32", "Not valid on Windows")
class ItimerTest(unittest.TestCase):
    def setUp(self):
        self.hndl_called = False
        self.hndl_count = 0
        self.itimer = None
        self.old_alarm = signal.signal(signal.SIGALRM, self.sig_alrm)

    def tearDown(self):
        signal.signal(signal.SIGALRM, self.old_alarm)
        if self.itimer is not None: # test_itimer_exc doesn't change this attr
            # just ensure that itimer is stopped
            signal.setitimer(self.itimer, 0)

    def sig_alrm(self, *args):
        self.hndl_called = True

    def sig_vtalrm(self, *args):
        self.hndl_called = True

        if self.hndl_count > 3:
            # it shouldn't be here, because it should have been disabled.
            raise signal.ItimerError("setitimer didn't disable ITIMER_VIRTUAL "
                "timer.")
        elif self.hndl_count == 3:
            # disable ITIMER_VIRTUAL, this function shouldn't be called anymore
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)

        self.hndl_count += 1

    def sig_prof(self, *args):
        self.hndl_called = True
        signal.setitimer(signal.ITIMER_PROF, 0)

    def test_itimer_exc(self):
        # XXX I'm assuming -1 is an invalid itimer, but maybe some platform
        # defines it ?
        self.assertRaises(signal.ItimerError, signal.setitimer, -1, 0)
        # Negative times are treated as zero on some platforms.
        if 0:
            self.assertRaises(signal.ItimerError,
                              signal.setitimer, signal.ITIMER_REAL, -1)

    def test_itimer_real(self):
        self.itimer = signal.ITIMER_REAL
        signal.setitimer(self.itimer, 1.0)
        signal.pause()
        self.assertEqual(self.hndl_called, True)

    # Issue 3864, unknown if this affects earlier versions of freebsd also
    @unittest.skipIf(sys.platform in ('freebsd6', 'netbsd5'),
        'itimer not reliable (does not mix well with threading) on some BSDs.')
    def test_itimer_virtual(self):
        self.itimer = signal.ITIMER_VIRTUAL
        signal.signal(signal.SIGVTALRM, self.sig_vtalrm)
        signal.setitimer(self.itimer, 0.3, 0.2)

        start_time = time.time()
        while time.time() - start_time < 60.0:
            # use up some virtual time by doing real work
            _ = pow(12345, 67890, 10000019)
            if signal.getitimer(self.itimer) == (0.0, 0.0):
                break # sig_vtalrm handler stopped this itimer
        else: # Issue 8424
            self.skipTest("timeout: likely cause: machine too slow or load too "
                          "high")

        # virtual itimer should be (0.0, 0.0) now
        self.assertEqual(signal.getitimer(self.itimer), (0.0, 0.0))
        # and the handler should have been called
        self.assertEqual(self.hndl_called, True)

    # Issue 3864, unknown if this affects earlier versions of freebsd also
    @unittest.skipIf(sys.platform=='freebsd6',
        'itimer not reliable (does not mix well with threading) on freebsd6')
    def test_itimer_prof(self):
        self.itimer = signal.ITIMER_PROF
        signal.signal(signal.SIGPROF, self.sig_prof)
        signal.setitimer(self.itimer, 0.2, 0.2)

        start_time = time.time()
        while time.time() - start_time < 60.0:
            # do some work
            _ = pow(12345, 67890, 10000019)
            if signal.getitimer(self.itimer) == (0.0, 0.0):
                break # sig_prof handler stopped this itimer
        else: # Issue 8424
            self.skipTest("timeout: likely cause: machine too slow or load too "
                          "high")

        # profiling itimer should be (0.0, 0.0) now
        self.assertEqual(signal.getitimer(self.itimer), (0.0, 0.0))
        # and the handler should have been called
        self.assertEqual(self.hndl_called, True)


class PendingSignalsTests(unittest.TestCase):
    """
    Test pthread_sigmask(), pthread_kill(), sigpending() and sigwait()
    functions.
    """
    def setUp(self):
        self.has_pthread_kill = hasattr(signal, 'pthread_kill')

    def handler(self, signum, frame):
        1/0

    def read_sigmask(self):
        return signal.pthread_sigmask(signal.SIG_BLOCK, [])

    def can_test_blocked_signals(self, skip):
        """
        Check if a blocked signal can be raised to the main thread without
        calling its signal handler. We need pthread_kill() or exactly one
        thread (the main thread).

        Return True if it's possible. Otherwise, return False and print a
        warning if skip is False, or raise a SkipTest exception if skip is
        True.
        """
        if self.has_pthread_kill:
            return True

        # The fault handler timeout thread masks all signals. If the main
        # thread masks also SIGUSR1, all threads mask this signal. In this
        # case, if we send SIGUSR1 to the process, the signal is pending in the
        # main or the faulthandler timeout thread.  Unblock SIGUSR1 in the main
        # thread calls the signal handler only if the signal is pending for the
        # main thread. Stop the faulthandler timeout thread to workaround this
        # problem.
        import faulthandler
        faulthandler.cancel_dump_tracebacks_later()

        # Issue #11998: The _tkinter module loads the Tcl library which
        # creates a thread waiting events in select(). This thread receives
        # signals blocked by all other threads. We cannot test blocked
        # signals
        if '_tkinter' in sys.modules:
            message = ("_tkinter is loaded and pthread_kill() is missing, "
                       "cannot test blocked signals (issue #11998)")
            if skip:
                self.skipTest(message)
            else:
                print("WARNING: %s" % message)
            return False
        return True

    def kill(self, signum):
        if self.has_pthread_kill:
            tid = threading.get_ident()
            signal.pthread_kill(tid, signum)
        else:
            pid = os.getpid()
            os.kill(pid, signum)

    @unittest.skipUnless(hasattr(signal, 'sigpending'),
                         'need signal.sigpending()')
    def test_sigpending_empty(self):
        self.assertEqual(signal.sigpending(), set())

    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'),
                         'need signal.pthread_sigmask()')
    @unittest.skipUnless(hasattr(signal, 'sigpending'),
                         'need signal.sigpending()')
    def test_sigpending(self):
        self.can_test_blocked_signals(True)

        signum = signal.SIGUSR1
        old_handler = signal.signal(signum, self.handler)
        self.addCleanup(signal.signal, signum, old_handler)

        signal.pthread_sigmask(signal.SIG_BLOCK, [signum])
        self.kill(signum)
        self.assertEqual(signal.sigpending(), {signum})
        with self.assertRaises(ZeroDivisionError):
            signal.pthread_sigmask(signal.SIG_UNBLOCK, [signum])

    @unittest.skipUnless(hasattr(signal, 'pthread_kill'),
                         'need signal.pthread_kill()')
    def test_pthread_kill(self):
        signum = signal.SIGUSR1
        current = threading.get_ident()

        old_handler = signal.signal(signum, self.handler)
        self.addCleanup(signal.signal, signum, old_handler)

        with self.assertRaises(ZeroDivisionError):
            signal.pthread_kill(current, signum)

    @unittest.skipUnless(hasattr(signal, 'sigwait'),
                         'need signal.sigwait()')
    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'),
                         'need signal.pthread_sigmask()')
    @unittest.skipUnless(hasattr(os, 'fork'), 'need os.fork()')
    def test_sigwait(self):
        def test(signum):
            signal.alarm(1)
            received = signal.sigwait([signum])
            if received != signum:
                print("sigwait() received %s, not %s"
                      % (received, signum),
                      file=sys.stderr)
                os._exit(1)

        signum = signal.SIGALRM

        # sigwait must be called with the signal blocked: since the current
        # process might have several threads running, we fork() a child process
        # to have a single thread.
        pid = os.fork()
        if pid == 0:
            # child: block and wait the signal
            try:
                signal.signal(signum, self.handler)
                signal.pthread_sigmask(signal.SIG_BLOCK, [signum])

                # Do the tests
                test(signum)

                # The handler must not be called on unblock
                try:
                    signal.pthread_sigmask(signal.SIG_UNBLOCK, [signum])
                except ZeroDivisionError:
                    print("the signal handler has been called",
                          file=sys.stderr)
                    os._exit(1)
            except BaseException as err:
                print("error: {}".format(err), file=sys.stderr)
                os._exit(1)
            else:
                os._exit(0)
        else:
            # parent: check that the child correcty received the signal
            self.assertEqual(os.waitpid(pid, 0), (pid, 0))

    @unittest.skipUnless(hasattr(signal, 'sigwait'),
                         'need signal.sigwait()')
    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'),
                         'need signal.pthread_sigmask()')
    @unittest.skipIf(threading is None, "test needs threading module")
    def test_sigwait_thread(self):
        # Check that calling sigwait() from a thread doesn't suspend the whole
        # process. A new interpreter is spawned to avoid problems when mixing
        # threads and fork(): only async-safe functions are allowed between
        # fork() and exec().
        assert_python_ok("-c", """if True:
            import os, threading, sys, time, signal

            # the default handler terminates the process
            signum = signal.SIGUSR1

            def kill_later():
                # wait until the main thread is waiting in sigwait()
                time.sleep(1)
                os.kill(os.getpid(), signum)

            # the signal must be blocked by all the threads
            signal.pthread_sigmask(signal.SIG_BLOCK, [signum])
            killer = threading.Thread(target=kill_later)
            killer.start()
            received = signal.sigwait([signum])
            if received != signum:
                print("sigwait() received %s, not %s" % (received, signum),
                      file=sys.stderr)
                sys.exit(1)
            killer.join()
            # unblock the signal, which should have been cleared by sigwait()
            signal.pthread_sigmask(signal.SIG_UNBLOCK, [signum])
        """)

    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'),
                         'need signal.pthread_sigmask()')
    def test_pthread_sigmask_arguments(self):
        self.assertRaises(TypeError, signal.pthread_sigmask)
        self.assertRaises(TypeError, signal.pthread_sigmask, 1)
        self.assertRaises(TypeError, signal.pthread_sigmask, 1, 2, 3)
        self.assertRaises(OSError, signal.pthread_sigmask, 1700, [])

    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'),
                         'need signal.pthread_sigmask()')
    def test_pthread_sigmask(self):
        test_blocked_signals = self.can_test_blocked_signals(False)
        signum = signal.SIGUSR1

        # Install our signal handler
        old_handler = signal.signal(signum, self.handler)
        self.addCleanup(signal.signal, signum, old_handler)

        # Unblock SIGUSR1 (and copy the old mask) to test our signal handler
        old_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, [signum])
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, old_mask)
        with self.assertRaises(ZeroDivisionError):
            self.kill(signum)

        # Block and then raise SIGUSR1. The signal is blocked: the signal
        # handler is not called, and the signal is now pending
        signal.pthread_sigmask(signal.SIG_BLOCK, [signum])
        if test_blocked_signals:
            self.kill(signum)

        # Check the new mask
        blocked = self.read_sigmask()
        self.assertIn(signum, blocked)
        self.assertEqual(old_mask ^ blocked, {signum})

        # Unblock SIGUSR1
        if test_blocked_signals:
            with self.assertRaises(ZeroDivisionError):
                # unblock the pending signal calls immediatly the signal handler
                signal.pthread_sigmask(signal.SIG_UNBLOCK, [signum])
        else:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, [signum])
        with self.assertRaises(ZeroDivisionError):
            self.kill(signum)

        # Check the new mask
        unblocked = self.read_sigmask()
        self.assertNotIn(signum, unblocked)
        self.assertEqual(blocked ^ unblocked, {signum})
        self.assertSequenceEqual(old_mask, unblocked)
        # Finally, restore the previous signal handler and the signal mask


def test_main():
    try:
        support.run_unittest(PosixTests, InterProcessSignalTests,
                             WakeupSignalTests, SiginterruptTest,
                             ItimerTest, WindowsSignalTests,
                             PendingSignalsTests)
    finally:
        support.reap_children()


if __name__ == "__main__":
    test_main()
