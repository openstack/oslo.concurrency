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
import weakref

import debtcollector
import fasteners
from oslo_config import cfg
from oslo_utils import reflection
from oslo_utils import timeutils

from oslo_concurrency._i18n import _

try:
    # import eventlet optionally
    import eventlet
    from eventlet import patcher as eventlet_patcher
except ImportError:
    eventlet = None
    eventlet_patcher = None


LOG = logging.getLogger(__name__)


_opts = [
    cfg.BoolOpt('disable_process_locking', default=False,
                help='Enables or disables inter-process locks.'),
    cfg.StrOpt('lock_path',
               default=os.environ.get("OSLO_LOCK_PATH"),
               help='Directory to use for lock files.  For security, the '
                    'specified directory should only be writable by the user '
                    'running the processes that need locking. '
                    'Defaults to environment variable OSLO_LOCK_PATH. '
                    'If external locks are used, a lock path must be set.')
]


def _register_opts(conf):
    conf.register_opts(_opts, group='oslo_concurrency')


CONF = cfg.CONF
_register_opts(CONF)


def set_defaults(lock_path):
    """Set value for lock_path.

    This can be used by tests to set lock_path to a temporary directory.
    """
    cfg.set_defaults(_opts, lock_path=lock_path)


def get_lock_path(conf):
    """Return the path used for external file-based locks.

    :param conf: Configuration object
    :type conf: oslo_config.cfg.ConfigOpts

    .. versionadded:: 1.8
    """
    _register_opts(conf)
    return conf.oslo_concurrency.lock_path


class ReaderWriterLock(fasteners.ReaderWriterLock):
    """A reader/writer lock.

    .. versionadded:: 0.4
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Until https://github.com/eventlet/eventlet/issues/731 is resolved
        # we need to use eventlet.getcurrent instead of
        # threading.current_thread if we are running in a monkey patched
        # environment
        if eventlet is not None and eventlet_patcher is not None:
            if eventlet_patcher.is_monkey_patched('thread'):
                debtcollector.deprecate(
                    "Eventlet support is deprecated and will be removed.")
                self._current_thread = eventlet.getcurrent


InterProcessLock = fasteners.InterProcessLock


class FairLocks:
    """A garbage collected container of fair locks.

    With a fair lock, contending lockers will get the lock in the order in
    which they tried to acquire it.

    This collection internally uses a weak value dictionary so that when a
    lock is no longer in use (by any threads) it will automatically be
    removed from this container by the garbage collector.
    """

    def __init__(self):
        self._locks = weakref.WeakValueDictionary()
        self._lock = threading.Lock()

    def get(self, name):
        """Gets (or creates) a lock with a given name.

        :param name: The lock name to get/create (used to associate
                     previously created names with the same lock).

        Returns an newly constructed lock (or an existing one if it was
        already created for the given name).
        """
        with self._lock:
            try:
                return self._locks[name]
            except KeyError:
                # The fasteners module specifies that
                # ReaderWriterLock.write_lock() will give FIFO behaviour,
                # so we don't need to do anything special ourselves.
                rwlock = ReaderWriterLock()
                self._locks[name] = rwlock
                return rwlock


_fair_locks = FairLocks()


def internal_fair_lock(name):
    return _fair_locks.get(name)


class Semaphores:
    """A garbage collected container of semaphores.

    This collection internally uses a weak value dictionary so that when a
    semaphore is no longer in use (by any threads) it will automatically be
    removed from this container by the garbage collector.

    .. versionadded:: 0.3
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
        name = '{}{}{}'.format(lock_file_prefix, sep, name)

    local_lock_path = lock_path or CONF.oslo_concurrency.lock_path

    if not local_lock_path:
        raise cfg.RequiredOptError('lock_path', 'oslo_concurrency')

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
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                LOG.warning('Failed to remove file %(file)s',
                            {'file': lock_file_path})


class AcquireLockFailedException(Exception):
    def __init__(self, lock_name):
        self.message = "Failed to acquire the lock %s" % lock_name

    def __str__(self):
        return self.message


def internal_lock(name, semaphores=None, blocking=True):
    @contextlib.contextmanager
    def nonblocking(lock):
        """Try to acquire the internal lock without blocking."""
        if not lock.acquire(blocking=False):
            raise AcquireLockFailedException(name)
        try:
            yield lock
        finally:
            lock.release()

    if semaphores is None:
        semaphores = _semaphores
    lock = semaphores.get(name)

    return nonblocking(lock) if not blocking else lock


@contextlib.contextmanager
def lock(name, lock_file_prefix=None, external=False, lock_path=None,
         do_log=True, semaphores=None, delay=0.01, fair=False, blocking=True):
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

    :param fair: Whether or not we want a "fair" lock where contending lockers
        will get the lock in the order in which they tried to acquire it.

    :param blocking: Whether to wait forever to try to acquire the lock.
        Incompatible with fair locks because those provided by the fasteners
        module doesn't implements a non-blocking behavior.

    .. versionchanged:: 0.2
       Added *do_log* optional parameter.

    .. versionchanged:: 0.3
       Added *delay* and *semaphores* optional parameters.
    """
    if fair:
        if semaphores is not None:
            raise NotImplementedError(_('Specifying semaphores is not '
                                        'supported when using fair locks.'))
        if blocking is not True:
            raise NotImplementedError(_('Disabling blocking is not supported '
                                        'when using fair locks.'))
        # The fasteners module specifies that write_lock() provides fairness.
        int_lock = internal_fair_lock(name).write_lock()
    else:
        int_lock = internal_lock(name, semaphores=semaphores,
                                 blocking=blocking)
    if do_log:
        LOG.debug('Acquiring lock "%s"', name)
    with int_lock:
        if do_log:
            LOG.debug('Acquired lock "%(lock)s"', {'lock': name})
        try:
            if external and not CONF.oslo_concurrency.disable_process_locking:
                ext_lock = external_lock(name, lock_file_prefix, lock_path)
                gotten = ext_lock.acquire(delay=delay, blocking=blocking)
                if not gotten:
                    raise AcquireLockFailedException(name)
                if do_log:
                    LOG.debug('Acquired external semaphore "%(lock)s"',
                              {'lock': name})
                try:
                    yield ext_lock
                finally:
                    ext_lock.release()
            else:
                yield int_lock
        finally:
            if do_log:
                LOG.debug('Releasing lock "%(lock)s"', {'lock': name})


def lock_with_prefix(lock_file_prefix):
    """Partial object generator for the lock context manager.

    Redefine lock in each project like so::

        (in nova/utils.py)
        from oslo_concurrency import lockutils

        _prefix = 'nova'
        lock = lockutils.lock_with_prefix(_prefix)
        lock_cleanup = lockutils.remove_external_lock_file_with_prefix(_prefix)


        (in nova/foo.py)
        from nova import utils

        with utils.lock('mylock'):
           ...

    Eventually clean up with::

        lock_cleanup('mylock')

    :param lock_file_prefix: A string used to provide lock files on disk with a
        meaningful prefix. Will be separated from the lock name with a hyphen,
        which may optionally be included in the lock_file_prefix (e.g.
        ``'nova'`` and ``'nova-'`` are equivalent).
    """
    return functools.partial(lock, lock_file_prefix=lock_file_prefix)


def synchronized(name, lock_file_prefix=None, external=False, lock_path=None,
                 semaphores=None, delay=0.01, fair=False, blocking=True):
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

    .. versionchanged:: 0.3
       Added *delay* and *semaphores* optional parameter.
    """

    def wrap(f):

        @functools.wraps(f)
        def inner(*args, **kwargs):
            t1 = timeutils.now()
            t2 = None
            gotten = True
            f_name = reflection.get_callable_name(f)
            try:
                LOG.debug('Acquiring lock "%s" by "%s"', name, f_name)
                with lock(name, lock_file_prefix, external, lock_path,
                          do_log=False, semaphores=semaphores, delay=delay,
                          fair=fair, blocking=blocking):
                    t2 = timeutils.now()
                    LOG.debug('Lock "%(name)s" acquired by "%(function)s" :: '
                              'waited %(wait_secs)0.3fs',
                              {'name': name,
                               'function': f_name,
                               'wait_secs': (t2 - t1)})
                    return f(*args, **kwargs)
            except AcquireLockFailedException:
                gotten = False
            finally:
                t3 = timeutils.now()
                if t2 is None:
                    held_secs = "N/A"
                else:
                    held_secs = "%0.3fs" % (t3 - t2)
                LOG.debug('Lock "%(name)s" "%(gotten)s" by "%(function)s" ::'
                          ' held %(held_secs)s',
                          {'name': name,
                           'gotten': 'released' if gotten else 'unacquired',
                           'function': f_name,
                           'held_secs': held_secs})
        return inner

    return wrap


def synchronized_with_prefix(lock_file_prefix):
    """Partial object generator for the synchronization decorator.

    Redefine @synchronized in each project like so::

        (in nova/utils.py)
        from oslo_concurrency import lockutils

        _prefix = 'nova'
        synchronized = lockutils.synchronized_with_prefix(_prefix)
        lock_cleanup = lockutils.remove_external_lock_file_with_prefix(_prefix)


        (in nova/foo.py)
        from nova import utils

        @utils.synchronized('mylock')
        def bar(self, *args):
           ...

    Eventually clean up with::

        lock_cleanup('mylock')

    :param lock_file_prefix: A string used to provide lock files on disk with a
        meaningful prefix. Will be separated from the lock name with a hyphen,
        which may optionally be included in the lock_file_prefix (e.g.
        ``'nova'`` and ``'nova-'`` are equivalent).
    """

    return functools.partial(synchronized, lock_file_prefix=lock_file_prefix)


def remove_external_lock_file_with_prefix(lock_file_prefix):
    """Partial object generator for the remove lock file function.

    Redefine remove_external_lock_file_with_prefix in each project like so::

        (in nova/utils.py)
        from oslo_concurrency import lockutils

        _prefix = 'nova'
        synchronized = lockutils.synchronized_with_prefix(_prefix)
        lock = lockutils.lock_with_prefix(_prefix)
        lock_cleanup = lockutils.remove_external_lock_file_with_prefix(_prefix)

        (in nova/foo.py)
        from nova import utils

        @utils.synchronized('mylock')
        def bar(self, *args):
            ...

        def baz(self, *args):
            ...
            with utils.lock('mylock'):
                ...
            ...

        <eventually call lock_cleanup('mylock') to clean up>

    The lock_file_prefix argument is used to provide lock files on disk with a
    meaningful prefix.
    """
    return functools.partial(remove_external_lock_file,
                             lock_file_prefix=lock_file_prefix)


def _lock_wrapper(argv):
    """Create a dir for locks and pass it to command from arguments

    This is exposed as a console script entry point named
    lockutils-wrapper

    If you run this:
        lockutils-wrapper stestr run <etc>

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


def main():
    sys.exit(_lock_wrapper(sys.argv))


if __name__ == '__main__':
    raise NotImplementedError(_('Calling lockutils directly is no longer '
                                'supported.  Please use the '
                                'lockutils-wrapper console script instead.'))
