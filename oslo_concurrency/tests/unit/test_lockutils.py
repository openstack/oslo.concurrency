#    Copyright 2011 Justin Santa Barbara
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import errno
import fcntl
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from oslo_config import cfg
from oslotest import base as test_base
import six

from concurrent import futures

from oslo_concurrency.fixture import lockutils as fixtures
from oslo_concurrency import lockutils
from oslo_config import fixture as config


class LockTestCase(test_base.BaseTestCase):

    def setUp(self):
        super(LockTestCase, self).setUp()
        self.config = self.useFixture(config.Config(lockutils.CONF)).config

    def test_synchronized_wrapped_function_metadata(self):
        @lockutils.synchronized('whatever', 'test-')
        def foo():
            """Bar."""
            pass

        self.assertEqual(foo.__doc__, 'Bar.', "Wrapped function's docstring "
                                              "got lost")
        self.assertEqual(foo.__name__, 'foo', "Wrapped function's name "
                                              "got mangled")

    def test_lock_acquire_release_file_lock(self):
        lock_dir = tempfile.mkdtemp()
        lock_file = os.path.join(lock_dir, 'lock')
        lock = lockutils._FcntlLock(lock_file)

        def try_lock():
            try:
                my_lock = lockutils._FcntlLock(lock_file)
                my_lock.lockfile = open(lock_file, 'w')
                my_lock.trylock()
                my_lock.unlock()
                os._exit(1)
            except IOError:
                os._exit(0)

        def attempt_acquire(count):
            children = []
            for i in range(count):
                child = multiprocessing.Process(target=try_lock)
                child.start()
                children.append(child)
            exit_codes = []
            for child in children:
                child.join()
                exit_codes.append(child.exitcode)
            return sum(exit_codes)

        self.assertTrue(lock.acquire())
        try:
            acquired_children = attempt_acquire(10)
            self.assertEqual(0, acquired_children)
        finally:
            lock.release()

        try:
            acquired_children = attempt_acquire(5)
            self.assertNotEqual(0, acquired_children)
        finally:
            try:
                shutil.rmtree(lock_dir)
            except IOError:
                pass

    def test_lock_internally_different_collections(self):
        s1 = lockutils.Semaphores()
        s2 = lockutils.Semaphores()
        trigger = threading.Event()
        who_ran = collections.deque()

        def f(name, semaphores, pull_trigger):
            with lockutils.internal_lock('testing', semaphores=semaphores):
                if pull_trigger:
                    trigger.set()
                else:
                    trigger.wait()
                who_ran.append(name)

        threads = [
            threading.Thread(target=f, args=(1, s1, True)),
            threading.Thread(target=f, args=(2, s2, False)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([1, 2], sorted(who_ran))

    def test_lock_internally(self):
        """We can lock across multiple threads."""
        saved_sem_num = len(lockutils._semaphores)
        seen_threads = list()

        def f(_id):
            with lockutils.lock('testlock2', 'test-', external=False):
                for x in range(10):
                    seen_threads.append(_id)

        threads = []
        for i in range(10):
            thread = threading.Thread(target=f, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        self.assertEqual(len(seen_threads), 100)
        # Looking at the seen threads, split it into chunks of 10, and verify
        # that the last 9 match the first in each chunk.
        for i in range(10):
            for j in range(9):
                self.assertEqual(seen_threads[i * 10],
                                 seen_threads[i * 10 + 1 + j])

        self.assertEqual(saved_sem_num, len(lockutils._semaphores),
                         "Semaphore leak detected")

    def test_nested_synchronized_external_works(self):
        """We can nest external syncs."""
        tempdir = tempfile.mkdtemp()
        try:
            self.config(lock_path=tempdir, group='oslo_concurrency')
            sentinel = object()

            @lockutils.synchronized('testlock1', 'test-', external=True)
            def outer_lock():

                @lockutils.synchronized('testlock2', 'test-', external=True)
                def inner_lock():
                    return sentinel
                return inner_lock()

            self.assertEqual(sentinel, outer_lock())

        finally:
            if os.path.exists(tempdir):
                shutil.rmtree(tempdir)

    def _do_test_lock_externally(self):
        """We can lock across multiple processes."""

        def lock_files(handles_dir):

            with lockutils.lock('external', 'test-', external=True):
                # Open some files we can use for locking
                handles = []
                for n in range(50):
                    path = os.path.join(handles_dir, ('file-%s' % n))
                    handles.append(open(path, 'w'))

                # Loop over all the handles and try locking the file
                # without blocking, keep a count of how many files we
                # were able to lock and then unlock. If the lock fails
                # we get an IOError and bail out with bad exit code
                count = 0
                for handle in handles:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        count += 1
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    except IOError:
                        os._exit(2)
                    finally:
                        handle.close()

                # Check if we were able to open all files
                self.assertEqual(50, count)

        handles_dir = tempfile.mkdtemp()
        try:
            children = []
            for n in range(50):
                pid = os.fork()
                if pid:
                    children.append(pid)
                else:
                    try:
                        lock_files(handles_dir)
                    finally:
                        os._exit(0)

            for child in children:
                (pid, status) = os.waitpid(child, 0)
                if pid:
                    self.assertEqual(0, status)
        finally:
            if os.path.exists(handles_dir):
                shutil.rmtree(handles_dir, ignore_errors=True)

    def test_lock_externally(self):
        lock_dir = tempfile.mkdtemp()
        self.config(lock_path=lock_dir, group='oslo_concurrency')

        try:
            self._do_test_lock_externally()
        finally:
            if os.path.exists(lock_dir):
                shutil.rmtree(lock_dir, ignore_errors=True)

    def test_lock_externally_lock_dir_not_exist(self):
        lock_dir = tempfile.mkdtemp()
        os.rmdir(lock_dir)
        self.config(lock_path=lock_dir, group='oslo_concurrency')

        try:
            self._do_test_lock_externally()
        finally:
            if os.path.exists(lock_dir):
                shutil.rmtree(lock_dir, ignore_errors=True)

    def test_synchronized_with_prefix(self):
        lock_name = 'mylock'
        lock_pfix = 'mypfix-'

        foo = lockutils.synchronized_with_prefix(lock_pfix)

        @foo(lock_name, external=True)
        def bar(dirpath, pfix, name):
            return True

        lock_dir = tempfile.mkdtemp()
        self.config(lock_path=lock_dir, group='oslo_concurrency')

        self.assertTrue(bar(lock_dir, lock_pfix, lock_name))

    def test_synchronized_without_prefix(self):
        lock_dir = tempfile.mkdtemp()
        self.config(lock_path=lock_dir, group='oslo_concurrency')

        @lockutils.synchronized('lock', external=True)
        def test_without_prefix():
            # We can't check much
            pass

        try:
            test_without_prefix()
        finally:
            if os.path.exists(lock_dir):
                shutil.rmtree(lock_dir, ignore_errors=True)

    def test_synchronized_prefix_without_hypen(self):
        lock_dir = tempfile.mkdtemp()
        self.config(lock_path=lock_dir, group='oslo_concurrency')

        @lockutils.synchronized('lock', 'hypen', True)
        def test_without_hypen():
            # We can't check much
            pass

        try:
            test_without_hypen()
        finally:
            if os.path.exists(lock_dir):
                shutil.rmtree(lock_dir, ignore_errors=True)

    def test_contextlock(self):
        lock_dir = tempfile.mkdtemp()
        self.config(lock_path=lock_dir, group='oslo_concurrency')

        try:
            # Note(flaper87): Lock is not external, which means
            # a semaphore will be yielded
            with lockutils.lock("test") as sem:
                if six.PY2:
                    self.assertTrue(isinstance(sem, threading._Semaphore))
                else:
                    self.assertTrue(isinstance(sem, threading.Semaphore))

                # NOTE(flaper87): Lock is external so an InterProcessLock
                # will be yielded.
                with lockutils.lock("test2", external=True) as lock:
                    self.assertTrue(lock.exists())

                with lockutils.lock("test1",
                                    external=True) as lock1:
                    self.assertTrue(isinstance(lock1,
                                               lockutils.InterProcessLock))
        finally:
            if os.path.exists(lock_dir):
                shutil.rmtree(lock_dir, ignore_errors=True)

    def test_contextlock_unlocks(self):
        lock_dir = tempfile.mkdtemp()
        self.config(lock_path=lock_dir, group='oslo_concurrency')

        sem = None

        try:
            with lockutils.lock("test") as sem:
                if six.PY2:
                    self.assertTrue(isinstance(sem, threading._Semaphore))
                else:
                    self.assertTrue(isinstance(sem, threading.Semaphore))

                with lockutils.lock("test2", external=True) as lock:
                    self.assertTrue(lock.exists())

                # NOTE(flaper87): Lock should be free
                with lockutils.lock("test2", external=True) as lock:
                    self.assertTrue(lock.exists())

            # NOTE(flaper87): Lock should be free
            # but semaphore should already exist.
            with lockutils.lock("test") as sem2:
                self.assertEqual(sem, sem2)
        finally:
            if os.path.exists(lock_dir):
                shutil.rmtree(lock_dir, ignore_errors=True)

    def _test_remove_lock_external_file(self, lock_dir, use_external=False):
        lock_name = 'mylock'
        lock_pfix = 'mypfix-remove-lock-test-'

        if use_external:
            lock_path = lock_dir
        else:
            lock_path = None

        lockutils.remove_external_lock_file(lock_name, lock_pfix, lock_path)

        for ent in os.listdir(lock_dir):
            self.assertRaises(OSError, ent.startswith, lock_pfix)

        if os.path.exists(lock_dir):
            shutil.rmtree(lock_dir, ignore_errors=True)

    def test_remove_lock_external_file(self):
        lock_dir = tempfile.mkdtemp()
        self.config(lock_path=lock_dir, group='oslo_concurrency')
        self._test_remove_lock_external_file(lock_dir)

    def test_remove_lock_external_file_lock_path(self):
        lock_dir = tempfile.mkdtemp()
        self._test_remove_lock_external_file(lock_dir,
                                             use_external=True)

    def test_no_slash_in_b64(self):
        # base64(sha1(foobar)) has a slash in it
        with lockutils.lock("foobar"):
            pass

    def test_deprecated_names(self):
        paths = self.create_tempfiles([['fake.conf', '\n'.join([
            '[DEFAULT]',
            'lock_path=foo',
            'disable_process_locking=True'])
        ]])
        conf = cfg.ConfigOpts()
        conf(['--config-file', paths[0]])
        conf.register_opts(lockutils._opts, 'oslo_concurrency')
        self.assertEqual(conf.oslo_concurrency.lock_path, 'foo')
        self.assertTrue(conf.oslo_concurrency.disable_process_locking)


class BrokenLock(lockutils._FileLock):
    def __init__(self, name, errno_code):
        super(BrokenLock, self).__init__(name)
        self.errno_code = errno_code

    def unlock(self):
        pass

    def trylock(self):
        err = IOError()
        err.errno = self.errno_code
        raise err


class FileBasedLockingTestCase(test_base.BaseTestCase):
    def setUp(self):
        super(FileBasedLockingTestCase, self).setUp()
        self.lock_dir = tempfile.mkdtemp()

    def test_lock_file_exists(self):
        lock_file = os.path.join(self.lock_dir, 'lock-file')

        @lockutils.synchronized('lock-file', external=True,
                                lock_path=self.lock_dir)
        def foo():
            self.assertTrue(os.path.exists(lock_file))

        foo()

    def test_bad_acquire(self):
        lock_file = os.path.join(self.lock_dir, 'lock')
        lock = BrokenLock(lock_file, errno.EBUSY)

        self.assertRaises(threading.ThreadError, lock.acquire)

    def test_interprocess_lock(self):
        lock_file = os.path.join(self.lock_dir, 'processlock')

        pid = os.fork()
        if pid:
            # Make sure the child grabs the lock first
            start = time.time()
            while not os.path.exists(lock_file):
                if time.time() - start > 5:
                    self.fail('Timed out waiting for child to grab lock')
                time.sleep(0)
            lock1 = lockutils.InterProcessLock('foo')
            lock1.lockfile = open(lock_file, 'w')
            # NOTE(bnemec): There is a brief window between when the lock file
            # is created and when it actually becomes locked.  If we happen to
            # context switch in that window we may succeed in locking the
            # file.  Keep retrying until we either get the expected exception
            # or timeout waiting.
            while time.time() - start < 5:
                try:
                    lock1.trylock()
                    lock1.unlock()
                    time.sleep(0)
                except IOError:
                    # This is what we expect to happen
                    break
            else:
                self.fail('Never caught expected lock exception')
            # We don't need to wait for the full sleep in the child here
            os.kill(pid, signal.SIGKILL)
        else:
            try:
                lock2 = lockutils.InterProcessLock('foo')
                lock2.lockfile = open(lock_file, 'w')
                have_lock = False
                while not have_lock:
                    try:
                        lock2.trylock()
                        have_lock = True
                    except IOError:
                        pass
            finally:
                # NOTE(bnemec): This is racy, but I don't want to add any
                # synchronization primitives that might mask a problem
                # with the one we're trying to test here.
                time.sleep(.5)
                os._exit(0)

    def test_interthread_external_lock(self):
        call_list = []

        @lockutils.synchronized('foo', external=True, lock_path=self.lock_dir)
        def foo(param):
            """Simulate a long-running threaded operation."""
            call_list.append(param)
            # NOTE(bnemec): This is racy, but I don't want to add any
            # synchronization primitives that might mask a problem
            # with the one we're trying to test here.
            time.sleep(.5)
            call_list.append(param)

        def other(param):
            foo(param)

        thread = threading.Thread(target=other, args=('other',))
        thread.start()
        # Make sure the other thread grabs the lock
        # NOTE(bnemec): File locks do not actually work between threads, so
        # this test is verifying that the local semaphore is still enforcing
        # external locks in that case.  This means this test does not have
        # the same race problem as the process test above because when the
        # file is created the semaphore has already been grabbed.
        start = time.time()
        while not os.path.exists(os.path.join(self.lock_dir, 'foo')):
            if time.time() - start > 5:
                self.fail('Timed out waiting for thread to grab lock')
            time.sleep(0)
        thread1 = threading.Thread(target=other, args=('main',))
        thread1.start()
        thread1.join()
        thread.join()
        self.assertEqual(call_list, ['other', 'other', 'main', 'main'])

    def test_non_destructive(self):
        lock_file = os.path.join(self.lock_dir, 'not-destroyed')
        with open(lock_file, 'w') as f:
            f.write('test')
        with lockutils.lock('not-destroyed', external=True,
                            lock_path=self.lock_dir):
            with open(lock_file) as f:
                self.assertEqual(f.read(), 'test')


class ReadWriteLockTest(test_base.BaseTestCase):
    # This test works by sending up a bunch of threads and then running
    # them all at once and having different threads either a read lock
    # or a write lock; and sleeping for a period of time while using it.
    #
    # After the tests have completed the timings of each thread are checked
    # to ensure that there are no *invalid* overlaps (a writer should never
    # overlap with any readers, for example).

    # We will spend this amount of time doing some "fake" work.
    WORK_TIMES = [(0.01 + x / 100.0) for x in range(0, 5)]

    # NOTE(harlowja): Sleep a little so time.time() can not be the same (which
    # will cause false positives when our overlap detection code runs). If
    # there are real overlaps then they will still exist.
    NAPPY_TIME = 0.05

    @staticmethod
    def _find_overlaps(times, start, end):
        """Counts num of overlaps between start and end in the given times."""
        overlaps = 0
        for (s, e) in times:
            if s >= start and e <= end:
                overlaps += 1
        return overlaps

    @classmethod
    def _spawn_variation(cls, readers, writers, max_workers=None):
        """Spawns the given number of readers and writers."""

        start_stops = collections.deque()
        lock = lockutils.ReaderWriterLock()

        def read_func(ident):
            with lock.read_lock():
                # TODO(harlowja): sometime in the future use a monotonic clock
                # here to avoid problems that can be caused by ntpd resyncing
                # the clock while we are actively running.
                enter_time = time.time()
                time.sleep(cls.WORK_TIMES[ident % len(cls.WORK_TIMES)])
                exit_time = time.time()
                start_stops.append((lock.READER, enter_time, exit_time))
                time.sleep(cls.NAPPY_TIME)

        def write_func(ident):
            with lock.write_lock():
                enter_time = time.time()
                time.sleep(cls.WORK_TIMES[ident % len(cls.WORK_TIMES)])
                exit_time = time.time()
                start_stops.append((lock.WRITER, enter_time, exit_time))
                time.sleep(cls.NAPPY_TIME)

        if max_workers is None:
            max_workers = max(0, readers) + max(0, writers)
        if max_workers > 0:
            with futures.ThreadPoolExecutor(max_workers=max_workers) as e:
                count = 0
                for _i in range(0, readers):
                    e.submit(read_func, count)
                    count += 1
                for _i in range(0, writers):
                    e.submit(write_func, count)
                    count += 1

        writer_times = []
        reader_times = []
        for (lock_type, start, stop) in list(start_stops):
            if lock_type == lock.WRITER:
                writer_times.append((start, stop))
            else:
                reader_times.append((start, stop))
        return (writer_times, reader_times)

    def test_writer_abort(self):
        # Ensures that the lock is released when the writer has an
        # exception...
        lock = lockutils.ReaderWriterLock()
        self.assertFalse(lock.owner_type)

        def blow_up():
            with lock.write_lock():
                self.assertEqual(lock.WRITER, lock.owner_type)
                raise RuntimeError("Broken")

        self.assertRaises(RuntimeError, blow_up)
        self.assertFalse(lock.owner_type)

    def test_reader_abort(self):
        lock = lockutils.ReaderWriterLock()
        self.assertFalse(lock.owner_type)

        def blow_up():
            with lock.read_lock():
                self.assertEqual(lock.READER, lock.owner_type)
                raise RuntimeError("Broken")

        self.assertRaises(RuntimeError, blow_up)
        self.assertFalse(lock.owner_type)

    def test_double_reader_abort(self):
        lock = lockutils.ReaderWriterLock()
        activated = collections.deque()

        def double_bad_reader():
            with lock.read_lock():
                with lock.read_lock():
                    raise RuntimeError("Broken")

        def happy_writer():
            with lock.write_lock():
                activated.append(lock.owner_type)

        # Submit a bunch of work to a pool, and then ensure that the correct
        # number of writers eventually executed (every other thread will
        # be a reader thread that will fail)...
        max_workers = 8
        with futures.ThreadPoolExecutor(max_workers=max_workers) as e:
            for i in range(0, max_workers):
                if i % 2 == 0:
                    e.submit(double_bad_reader)
                else:
                    e.submit(happy_writer)

        self.assertEqual(max_workers / 2,
                         len([a for a in activated
                              if a == lockutils.ReaderWriterLock.WRITER]))

    def test_double_reader_writer(self):
        lock = lockutils.ReaderWriterLock()
        activated = collections.deque()
        active = threading.Event()

        def double_reader():
            with lock.read_lock():
                active.set()
                # Wait for the writer thread to get into pending mode using a
                # simple spin-loop...
                while not lock._has_pending_writers():
                    time.sleep(0.001)
                with lock.read_lock():
                    activated.append(lock.owner_type)

        def happy_writer():
            with lock.write_lock():
                activated.append(lock.owner_type)

        reader = threading.Thread(target=double_reader)
        reader.daemon = True
        reader.start()

        # Wait for the reader to become the active reader.
        active.wait()
        self.assertTrue(active.is_set())

        # Start up the writer (the reader will wait until its going).
        writer = threading.Thread(target=happy_writer)
        writer.daemon = True
        writer.start()

        # Ensure it went in the order we expected.
        reader.join()
        writer.join()
        self.assertEqual(2, len(activated))
        self.assertEqual([lockutils.ReaderWriterLock.READER,
                          lockutils.ReaderWriterLock.WRITER], list(activated))

    def test_reader_chaotic(self):
        lock = lockutils.ReaderWriterLock()
        activated = collections.deque()

        def chaotic_reader(blow_up):
            with lock.read_lock():
                if blow_up:
                    raise RuntimeError("Broken")
                else:
                    activated.append(lock.owner_type)

        def happy_writer():
            with lock.write_lock():
                activated.append(lock.owner_type)

        # Test that every 4th reader blows up and that we get the expected
        # number of owners with this occuring.
        max_workers = 8
        with futures.ThreadPoolExecutor(max_workers=max_workers) as e:
            for i in range(0, max_workers):
                if i % 2 == 0:
                    e.submit(chaotic_reader, blow_up=bool(i % 4 == 0))
                else:
                    e.submit(happy_writer)

        writers = [a for a in activated
                   if a == lockutils.ReaderWriterLock.WRITER]
        readers = [a for a in activated
                   if a == lockutils.ReaderWriterLock.READER]
        self.assertEqual(4, len(writers))
        self.assertEqual(2, len(readers))

    def test_writer_chaotic(self):
        lock = lockutils.ReaderWriterLock()
        activated = collections.deque()

        def chaotic_writer(blow_up):
            with lock.write_lock():
                if blow_up:
                    raise RuntimeError("Broken")
                else:
                    activated.append(lock.owner_type)

        def happy_reader():
            with lock.read_lock():
                activated.append(lock.owner_type)

        # Test that every 4th reader blows up and that we get the expected
        # number of owners with this occuring.
        max_workers = 8
        with futures.ThreadPoolExecutor(max_workers=max_workers) as e:
            for i in range(0, max_workers):
                if i % 2 == 0:
                    e.submit(chaotic_writer, blow_up=bool(i % 4 == 0))
                else:
                    e.submit(happy_reader)

        writers = [a for a in activated
                   if a == lockutils.ReaderWriterLock.WRITER]
        readers = [a for a in activated
                   if a == lockutils.ReaderWriterLock.READER]
        self.assertEqual(2, len(writers))
        self.assertEqual(4, len(readers))

    def test_single_reader_writer(self):
        results = []
        lock = lockutils.ReaderWriterLock()
        with lock.read_lock():
            self.assertTrue(lock._is_reader())
            self.assertEqual(0, len(results))
        with lock.write_lock():
            results.append(1)
            self.assertTrue(lock._is_writer())
        with lock.read_lock():
            self.assertTrue(lock._is_reader())
            self.assertEqual(1, len(results))
        self.assertFalse(lock._is_reader())
        self.assertFalse(lock._is_writer())

    def test_reader_to_writer(self):
        lock = lockutils.ReaderWriterLock()

        def writer_func():
            with lock.write_lock():
                pass

        with lock.read_lock():
            self.assertRaises(RuntimeError, writer_func)
            self.assertFalse(lock._is_writer())

        self.assertFalse(lock._is_reader())
        self.assertFalse(lock._is_writer())

    def test_writer_to_reader(self):
        lock = lockutils.ReaderWriterLock()

        def reader_func():
            with lock.read_lock():
                pass

        with lock.write_lock():
            self.assertRaises(RuntimeError, reader_func)
            self.assertFalse(lock._is_reader())

        self.assertFalse(lock._is_reader())
        self.assertFalse(lock._is_writer())

    def test_double_writer(self):
        lock = lockutils.ReaderWriterLock()
        with lock.write_lock():
            self.assertFalse(lock._is_reader())
            self.assertTrue(lock._is_writer())
            with lock.write_lock():
                self.assertTrue(lock._is_writer())
            self.assertTrue(lock._is_writer())

        self.assertFalse(lock._is_reader())
        self.assertFalse(lock._is_writer())

    def test_double_reader(self):
        lock = lockutils.ReaderWriterLock()
        with lock.read_lock():
            self.assertTrue(lock._is_reader())
            self.assertFalse(lock._is_writer())
            with lock.read_lock():
                self.assertTrue(lock._is_reader())
            self.assertTrue(lock._is_reader())

        self.assertFalse(lock._is_reader())
        self.assertFalse(lock._is_writer())

    def test_multi_reader_multi_writer(self):
        writer_times, reader_times = self._spawn_variation(10, 10)
        self.assertEqual(10, len(writer_times))
        self.assertEqual(10, len(reader_times))
        for (start, stop) in writer_times:
            self.assertEqual(0, self._find_overlaps(reader_times, start, stop))
            self.assertEqual(1, self._find_overlaps(writer_times, start, stop))
        for (start, stop) in reader_times:
            self.assertEqual(0, self._find_overlaps(writer_times, start, stop))

    def test_multi_reader_single_writer(self):
        writer_times, reader_times = self._spawn_variation(9, 1)
        self.assertEqual(1, len(writer_times))
        self.assertEqual(9, len(reader_times))
        start, stop = writer_times[0]
        self.assertEqual(0, self._find_overlaps(reader_times, start, stop))

    def test_multi_writer(self):
        writer_times, reader_times = self._spawn_variation(0, 10)
        self.assertEqual(10, len(writer_times))
        self.assertEqual(0, len(reader_times))
        for (start, stop) in writer_times:
            self.assertEqual(1, self._find_overlaps(writer_times, start, stop))


class LockutilsModuleTestCase(test_base.BaseTestCase):

    def setUp(self):
        super(LockutilsModuleTestCase, self).setUp()
        self.old_env = os.environ.get('OSLO_LOCK_PATH')
        if self.old_env is not None:
            del os.environ['OSLO_LOCK_PATH']

    def tearDown(self):
        if self.old_env is not None:
            os.environ['OSLO_LOCK_PATH'] = self.old_env
        super(LockutilsModuleTestCase, self).tearDown()

    def test_main(self):
        script = '\n'.join([
            'import os',
            'lock_path = os.environ.get("OSLO_LOCK_PATH")',
            'assert lock_path is not None',
            'assert os.path.isdir(lock_path)',
        ])
        argv = ['', sys.executable, '-c', script]
        retval = lockutils._lock_wrapper(argv)
        self.assertEqual(retval, 0, "Bad OSLO_LOCK_PATH has been set")

    def test_return_value_maintained(self):
        script = '\n'.join([
            'import sys',
            'sys.exit(1)',
        ])
        argv = ['', sys.executable, '-c', script]
        retval = lockutils._lock_wrapper(argv)
        self.assertEqual(retval, 1)

    def test_direct_call_explodes(self):
        cmd = [sys.executable, '-m', 'oslo_concurrency.lockutils']
        with open(os.devnull, 'w') as devnull:
            retval = subprocess.call(cmd, stderr=devnull)
            # 1 for Python 2.7 and 3.x, 255 for 2.6
            self.assertIn(retval, [1, 255])


class TestLockFixture(test_base.BaseTestCase):

    def setUp(self):
        super(TestLockFixture, self).setUp()
        self.config = self.useFixture(config.Config(lockutils.CONF)).config
        self.tempdir = tempfile.mkdtemp()

    def _check_in_lock(self):
        self.assertTrue(self.lock.exists())

    def tearDown(self):
        self._check_in_lock()
        super(TestLockFixture, self).tearDown()

    def test_lock_fixture(self):
        # Setup lock fixture to test that teardown is inside the lock
        self.config(lock_path=self.tempdir, group='oslo_concurrency')
        fixture = fixtures.LockFixture('test-lock')
        self.useFixture(fixture)
        self.lock = fixture.lock
