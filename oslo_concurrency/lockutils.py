# Copyright 2011 OpenStack Foundation.
# All Rights Reserved.
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
import contextlib
import errno
import functools
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref

from oslo_config import cfg
import retrying
import six

from oslo_concurrency._i18n import _, _LE, _LI
from oslo_concurrency.openstack.common import fileutils


LOG = logging.getLogger(__name__)


_opts = [
    cfg.BoolOpt('disable_process_locking', default=False,
                help='Enables or disables inter-process locks.',
                deprecated_group='DEFAULT'),
    cfg.StrOpt('lock_path',
               default=os.environ.get("OSLO_LOCK_PATH"),
               help='Directory to use for lock files.  For security, the '
                    'specified directory should only be writable by the user '
                    'running the processes that need locking. '
                    'Defaults to environment variable OSLO_LOCK_PATH. '
                    'If external locks are used, a lock path must be set.',
               deprecated_group='DEFAULT')
]


CONF = cfg.CONF
CONF.register_opts(_opts, group='oslo_concurrency')


def set_defaults(lock_path):
    """Set value for lock_path.

    This can be used by tests to set lock_path to a temporary directory.
    """
    cfg.set_defaults(_opts, lock_path=lock_path)


class _Hourglass(object):
    """A hourglass like periodic timer."""

    def __init__(self, period):
        self._period = period
        self._last_flipped = None

    def flip(self):
        """Flips the hourglass.

        The drain() method will now only return true until the period
        is reached again.
        """
        self._last_flipped = time.time()

    def drain(self):
        """Drains the hourglass, returns True if period reached."""
        if self._last_flipped is None:
            return True
        else:
            elapsed = max(0, time.time() - self._last_flipped)
            return elapsed >= self._period


def _lock_retry(delay, filename,
                # These parameters trigger logging to begin after a certain
                # amount of time has elapsed where the lock couldn't be
                # acquired (log statements will be emitted after that duration
                # at the provided periodicity).
                log_begins_after=1.0, log_periodicity=0.5):
    """Retry logic that acquiring a lock will go through."""

    # If this returns True, a retry attempt will occur (using the defined
    # retry policy we have requested the retrying library to apply), if it
    # returns False then the original exception will be re-raised (if it
    # raises a new or different exception the original exception will be
    # replaced with that one and raised).
    def retry_on_exception(e):
        # TODO(harlowja): once/if https://github.com/rholder/retrying/pull/20
        # gets merged we should just switch to using that to avoid having to
        # catch and inspect all execeptions (and there types...)
        if isinstance(e, IOError) and e.errno in (errno.EACCES, errno.EAGAIN):
            return True
        raise threading.ThreadError(_("Unable to acquire lock on"
                                      " `%(filename)s` due to"
                                      " %(exception)s") %
                                    {
                                        'filename': filename,
                                        'exception': e,
                                    })

    # Logs all attempts (with information about how long we have been trying
    # to acquire the underlying lock...); after a threshold has been passed,
    # and only at a fixed rate...
    def never_stop(hg, attempt_number, delay_since_first_attempt_ms):
        delay_since_first_attempt = delay_since_first_attempt_ms / 1000.0
        if delay_since_first_attempt >= log_begins_after:
            if hg.drain():
                LOG.debug("Attempting to acquire %s (delayed %0.2f seconds)",
                          filename, delay_since_first_attempt)
                hg.flip()
        return False

    # The retrying library seems to prefer milliseconds for some reason; this
    # might be changed in (see: https://github.com/rholder/retrying/issues/6)
    # someday in the future...
    delay_ms = delay * 1000.0

    def decorator(func):

        @six.wraps(func)
        def wrapper(*args, **kwargs):
            hg = _Hourglass(log_periodicity)
            r = retrying.Retrying(wait_fixed=delay_ms,
                                  retry_on_exception=retry_on_exception,
                                  stop_func=functools.partial(never_stop, hg))
            return r.call(func, *args, **kwargs)

        return wrapper

    return decorator


class _FileLock(object):
    """Lock implementation which allows multiple locks, working around
    issues like bugs.debian.org/cgi-bin/bugreport.cgi?bug=632857 and does
    not require any cleanup. Since the lock is always held on a file
    descriptor rather than outside of the process, the lock gets dropped
    automatically if the process crashes, even if __exit__ is not executed.

    There are no guarantees regarding usage by multiple green threads in a
    single process here. This lock works only between processes. Exclusive
    access between local threads should be achieved using the semaphores
    in the @synchronized decorator.

    Note these locks are released when the descriptor is closed, so it's not
    safe to close the file descriptor while another green thread holds the
    lock. Just opening and closing the lock file can break synchronisation,
    so lock files must be accessed only using this abstraction.
    """

    def __init__(self, name):
        self.lockfile = None
        self.fname = name
        self.acquire_time = None

    def acquire(self, delay=0.01):
        if delay < 0:
            raise ValueError("Delay must be greater than or equal to zero")

        basedir = os.path.dirname(self.fname)
        if not os.path.exists(basedir):
            fileutils.ensure_tree(basedir)
            LOG.info(_LI('Created lock path: %s'), basedir)

        # Open in append mode so we don't overwrite any potential contents of
        # the target file.  This eliminates the possibility of an attacker
        # creating a symlink to an important file in our lock_path.
        self.lockfile = open(self.fname, 'a')
        start_time = time.time()

        # Using non-blocking locks (with retries) since green threads are not
        # patched to deal with blocking locking calls. Also upon reading the
        # MSDN docs for locking(), it seems to have a 'laughable' 10
        # attempts "blocking" mechanism.
        do_acquire = _lock_retry(delay=delay,
                                 filename=self.fname)(self.trylock)
        do_acquire()
        self.acquire_time = time.time()
        LOG.debug('Acquired file lock "%s" after waiting %0.3fs',
                  self.fname, (self.acquire_time - start_time))

        return True

    def __enter__(self):
        self.acquire()
        return self

    def release(self):
        if self.acquire_time is None:
            raise threading.ThreadError(_("Unable to release an unacquired"
                                          " lock"))
        try:
            release_time = time.time()
            LOG.debug('Releasing file lock "%s" after holding it for %0.3fs',
                      self.fname, (release_time - self.acquire_time))
            self.unlock()
            self.acquire_time = None
        except IOError:
            LOG.exception(_LE("Could not unlock the acquired lock `%s`"),
                          self.fname)
        else:
            try:
                self.lockfile.close()
            except IOError:
                LOG.exception(_LE("Could not close the acquired file handle"
                                  " `%s`"), self.fname)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def exists(self):
        return os.path.exists(self.fname)

    def trylock(self):
        raise NotImplementedError()

    def unlock(self):
        raise NotImplementedError()


class _WindowsLock(_FileLock):
    def trylock(self):
        msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)

    def unlock(self):
        msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_UNLCK, 1)


class _FcntlLock(_FileLock):
    def trylock(self):
        fcntl.lockf(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(self):
        fcntl.lockf(self.lockfile, fcntl.LOCK_UN)


if os.name == 'nt':
    import msvcrt
    InterProcessLock = _WindowsLock
else:
    import fcntl
    InterProcessLock = _FcntlLock


class Semaphores(object):
    """A garbage collected container of semaphores.

    This collection internally uses a weak value dictionary so that when a
    semaphore is no longer in use (by any threads) it will automatically be
    removed from this container by the garbage collector.
    """

    def __init__(self):
        self._semaphores = weakref.WeakValueDictionary()
        self._lock = threading.Lock()

    def get(self, name):
        """Gets (or creates) a semaphore with a given name.

        :param name: The semaphore name to get/create (used to associate
                     previously created names with the same semaphore).

        Returns an newly constructed semaphore (or an existing one if it was
        already created for the given name).
        """
        with self._lock:
            try:
                return self._semaphores[name]
            except KeyError:
                sem = threading.Semaphore()
                self._semaphores[name] = sem
                return sem

    def __len__(self):
        """Returns how many semaphores exist at the current time."""
        return len(self._semaphores)


_semaphores = Semaphores()


def _get_lock_path(name, lock_file_prefix, lock_path=None):
    # NOTE(mikal): the lock name cannot contain directory
    # separators
    name = name.replace(os.sep, '_')
    if lock_file_prefix:
        sep = '' if lock_file_prefix.endswith('-') else '-'
        name = '%s%s%s' % (lock_file_prefix, sep, name)

    local_lock_path = lock_path or CONF.oslo_concurrency.lock_path

    if not local_lock_path:
        raise cfg.RequiredOptError('lock_path')

    return os.path.join(local_lock_path, name)


def external_lock(name, lock_file_prefix=None, lock_path=None):
    lock_file_path = _get_lock_path(name, lock_file_prefix, lock_path)

    return InterProcessLock(lock_file_path)


def remove_external_lock_file(name, lock_file_prefix=None, lock_path=None,
                              semaphores=None):
    """Remove an external lock file when it's not used anymore
    This will be helpful when we have a lot of lock files
    """
    with internal_lock(name, semaphores=semaphores):
        lock_file_path = _get_lock_path(name, lock_file_prefix, lock_path)
        try:
            os.remove(lock_file_path)
        except OSError:
            LOG.info(_LI('Failed to remove file %(file)s'),
                     {'file': lock_file_path})


def internal_lock(name, semaphores=None):
    if semaphores is None:
        semaphores = _semaphores
    return semaphores.get(name)


@contextlib.contextmanager
def lock(name, lock_file_prefix=None, external=False, lock_path=None,
         do_log=True, semaphores=None, delay=0.01):
    """Context based lock

    This function yields a `threading.Semaphore` instance (if we don't use
    eventlet.monkey_patch(), else `semaphore.Semaphore`) unless external is
    True, in which case, it'll yield an InterProcessLock instance.

    :param lock_file_prefix: The lock_file_prefix argument is used to provide
      lock files on disk with a meaningful prefix.

    :param external: The external keyword argument denotes whether this lock
      should work across multiple processes. This means that if two different
      workers both run a method decorated with @synchronized('mylock',
      external=True), only one of them will execute at a time.

    :param lock_path: The path in which to store external lock files.  For
      external locking to work properly, this must be the same for all
      references to the lock.

    :param do_log: Whether to log acquire/release messages.  This is primarily
      intended to reduce log message duplication when `lock` is used from the
      `synchronized` decorator.

    :param semaphores: Container that provides semaphores to use when locking.
        This ensures that threads inside the same application can not collide,
        due to the fact that external process locks are unaware of a processes
        active threads.

    :param delay: Delay between acquisition attempts (in seconds).
    """
    int_lock = internal_lock(name, semaphores=semaphores)
    with int_lock:
        if do_log:
            LOG.debug('Acquired semaphore "%(lock)s"', {'lock': name})
        try:
            if external and not CONF.oslo_concurrency.disable_process_locking:
                ext_lock = external_lock(name, lock_file_prefix, lock_path)
                ext_lock.acquire(delay=delay)
                try:
                    yield ext_lock
                finally:
                    ext_lock.release()
            else:
                yield int_lock
        finally:
            if do_log:
                LOG.debug('Releasing semaphore "%(lock)s"', {'lock': name})


def synchronized(name, lock_file_prefix=None, external=False, lock_path=None,
                 semaphores=None, delay=0.01):
    """Synchronization decorator.

    Decorating a method like so::

        @synchronized('mylock')
        def foo(self, *args):
           ...

    ensures that only one thread will execute the foo method at a time.

    Different methods can share the same lock::

        @synchronized('mylock')
        def foo(self, *args):
           ...

        @synchronized('mylock')
        def bar(self, *args):
           ...

    This way only one of either foo or bar can be executing at a time.
    """

    def wrap(f):
        @six.wraps(f)
        def inner(*args, **kwargs):
            t1 = time.time()
            t2 = None
            try:
                with lock(name, lock_file_prefix, external, lock_path,
                          do_log=False, semaphores=semaphores, delay=delay):
                    t2 = time.time()
                    LOG.debug('Lock "%(name)s" acquired by "%(function)s" :: '
                              'waited %(wait_secs)0.3fs',
                              {'name': name, 'function': f.__name__,
                               'wait_secs': (t2 - t1)})
                    return f(*args, **kwargs)
            finally:
                t3 = time.time()
                if t2 is None:
                    held_secs = "N/A"
                else:
                    held_secs = "%0.3fs" % (t3 - t2)

                LOG.debug('Lock "%(name)s" released by "%(function)s" :: held '
                          '%(held_secs)s',
                          {'name': name, 'function': f.__name__,
                           'held_secs': held_secs})
        return inner
    return wrap


def synchronized_with_prefix(lock_file_prefix):
    """Partial object generator for the synchronization decorator.

    Redefine @synchronized in each project like so::

        (in nova/utils.py)
        from nova.openstack.common import lockutils

        synchronized = lockutils.synchronized_with_prefix('nova-')


        (in nova/foo.py)
        from nova import utils

        @utils.synchronized('mylock')
        def bar(self, *args):
           ...

    The lock_file_prefix argument is used to provide lock files on disk with a
    meaningful prefix.
    """

    return functools.partial(synchronized, lock_file_prefix=lock_file_prefix)


def _lock_wrapper(argv):
    """Create a dir for locks and pass it to command from arguments

    This is exposed as a console script entry point named
    lockutils-wrapper

    If you run this:
        lockutils-wrapper python setup.py testr <etc>

    a temporary directory will be created for all your locks and passed to all
    your tests in an environment variable. The temporary dir will be deleted
    afterwards and the return value will be preserved.
    """

    lock_dir = tempfile.mkdtemp()
    os.environ["OSLO_LOCK_PATH"] = lock_dir
    try:
        ret_val = subprocess.call(argv[1:])
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)
    return ret_val


class ReaderWriterLock(object):
    """A reader/writer lock.

    This lock allows for simultaneous readers to exist but only one writer
    to exist for use-cases where it is useful to have such types of locks.

    Currently a reader can not escalate its read lock to a write lock and
    a writer can not acquire a read lock while it owns or is waiting on
    the write lock.

    In the future these restrictions may be relaxed.

    This can be eventually removed if http://bugs.python.org/issue8800 ever
    gets accepted into the python standard threading library...
    """
    WRITER = b'w'
    READER = b'r'

    @staticmethod
    def _fetch_current_thread_functor():
        # Until https://github.com/eventlet/eventlet/issues/172 is resolved
        # or addressed we have to use complicated workaround to get a object
        # that will not be recycled; the usage of threading.current_thread()
        # doesn't appear to currently be monkey patched and therefore isn't
        # reliable to use (and breaks badly when used as all threads share
        # the same current_thread() object)...
        try:
            import eventlet
            from eventlet import patcher
            green_threaded = patcher.is_monkey_patched('thread')
        except ImportError:
            green_threaded = False
        if green_threaded:
            return lambda: eventlet.getcurrent()
        else:
            return lambda: threading.current_thread()

    def __init__(self):
        self._writer = None
        self._pending_writers = collections.deque()
        self._readers = collections.defaultdict(int)
        self._cond = threading.Condition()
        self._current_thread = self._fetch_current_thread_functor()

    def _has_pending_writers(self):
        """Returns if there are writers waiting to become the *one* writer.

        Internal usage only.

        :return: whether there are any pending writers
        :rtype: boolean
        """
        return bool(self._pending_writers)

    def _is_writer(self, check_pending=True):
        """Returns if the caller is the active writer or a pending writer.

        Internal usage only.

        :param check_pending: checks the pending writes as well, if false then
                              only the current writer is checked (and not those
                              writers that may be in line).

        :return: whether the current thread is a active/pending writer
        :rtype: boolean
        """
        me = self._current_thread()
        with self._cond:
            if self._writer is not None and self._writer == me:
                return True
            if check_pending:
                return me in self._pending_writers
            else:
                return False

    @property
    def owner_type(self):
        """Returns whether the lock is locked by a writer/reader/nobody.

        :return: constant defining what the active owners type is
        :rtype: WRITER/READER/None
        """
        with self._cond:
            if self._writer is not None:
                return self.WRITER
            if self._readers:
                return self.READER
            return None

    def _is_reader(self):
        """Returns if the caller is one of the readers.

        Internal usage only.

        :return: whether the current thread is a active/pending reader
        :rtype: boolean
        """
        me = self._current_thread()
        with self._cond:
            return me in self._readers

    @contextlib.contextmanager
    def read_lock(self):
        """Context manager that grants a read lock.

        Will wait until no active or pending writers.

        Raises a ``RuntimeError`` if an active or pending writer tries to
        acquire a read lock as this is disallowed.
        """
        me = self._current_thread()
        if self._is_writer():
            raise RuntimeError("Writer %s can not acquire a read lock"
                               " while holding/waiting for the write lock"
                               % me)
        with self._cond:
            while self._writer is not None:
                # An active writer; guess we have to wait.
                self._cond.wait()
            # No active writer; we are good to become a reader.
            self._readers[me] += 1
        try:
            yield self
        finally:
            # I am no longer a reader, remove *one* occurrence of myself.
            # If the current thread acquired two read locks, then it will
            # still have to remove that other read lock; this allows for
            # basic reentrancy to be possible.
            with self._cond:
                claims = self._readers[me]
                if claims == 1:
                    self._readers.pop(me)
                else:
                    self._readers[me] = claims - 1
                if not self._readers:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def write_lock(self):
        """Context manager that grants a write lock.

        Will wait until no active readers. Blocks readers after acquiring.

        Raises a ``RuntimeError`` if an active reader attempts to acquire a
        writer lock as this is disallowed.
        """
        me = self._current_thread()
        if self._is_reader():
            raise RuntimeError("Reader %s to writer privilege"
                               " escalation not allowed" % me)
        if self._is_writer(check_pending=False):
            # Already the writer; this allows for basic reentrancy.
            yield self
        else:
            with self._cond:
                # Add ourself to the pending writes and wait until we are
                # the one writer that can run (aka, when we are the first
                # element in the pending writers).
                self._pending_writers.append(me)
                while (self._readers or self._writer is not None
                       or self._pending_writers[0] != me):
                    self._cond.wait()
                self._writer = self._pending_writers.popleft()
            try:
                yield self
            finally:
                with self._cond:
                    self._writer = None
                    self._cond.notify_all()


def main():
    sys.exit(_lock_wrapper(sys.argv))


if __name__ == '__main__':
    raise NotImplementedError(_('Calling lockutils directly is no longer '
                                'supported.  Please use the '
                                'lockutils-wrapper console script instead.'))
