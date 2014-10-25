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
import time
import weakref

from oslo.config import cfg
from oslo.config import cfgfilter
import six

from oslo.concurrency._i18n import _, _LE, _LI
from oslo.concurrency.openstack.common import fileutils


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


CONF = cfgfilter.ConfigFilter(cfg.CONF)
CONF.register_opts(_opts, group='oslo_concurrency')


def set_defaults(lock_path):
    """Set value for lock_path.

    This can be used by tests to set lock_path to a temporary directory.
    """
    cfg.set_defaults(_opts, lock_path=lock_path)


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

    def acquire(self):
        basedir = os.path.dirname(self.fname)

        if not os.path.exists(basedir):
            fileutils.ensure_tree(basedir)
            LOG.info(_LI('Created lock path: %s'), basedir)

        # Open in append mode so we don't overwrite any potential contents of
        # the target file.  This eliminates the possibility of an attacker
        # creating a symlink to an important file in our lock_path.
        self.lockfile = open(self.fname, 'a')

        start_time = time.time()
        while True:
            try:
                # Using non-blocking locks since green threads are not
                # patched to deal with blocking locking calls.
                # Also upon reading the MSDN docs for locking(), it seems
                # to have a laughable 10 attempts "blocking" mechanism.
                self.trylock()
                self.acquire_time = time.time()
                LOG.debug('Acquired file lock "%s" after waiting %0.3fs',
                          self.fname, (self.acquire_time - start_time))
                return True
            except IOError as e:
                if e.errno in (errno.EACCES, errno.EAGAIN):
                    # external locks synchronise things like iptables
                    # updates - give it some time to prevent busy spinning
                    time.sleep(0.01)
                else:
                    raise threading.ThreadError(_("Unable to acquire lock on"
                                                  " `%(filename)s` due to"
                                                  " %(exception)s") %
                                                {
                                                    'filename': self.fname,
                                                    'exception': e,
                                                })

    def __enter__(self):
        self.acquire()
        return self

    def release(self):
        try:
            release_time = time.time()
            LOG.debug('Releasing file lock "%s" after holding it for %0.3fs',
                      self.fname, (release_time - self.acquire_time))
            self.unlock()
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

_semaphores = weakref.WeakValueDictionary()
_semaphores_lock = threading.Lock()


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


def remove_external_lock_file(name, lock_file_prefix=None, lock_path=None):
    """Remove an external lock file when it's not used anymore
    This will be helpful when we have a lot of lock files
    """
    with internal_lock(name):
        lock_file_path = _get_lock_path(name, lock_file_prefix, lock_path)
        try:
            os.remove(lock_file_path)
        except OSError:
            LOG.info(_LI('Failed to remove file %(file)s'),
                     {'file': lock_file_path})


def internal_lock(name):
    with _semaphores_lock:
        try:
            sem = _semaphores[name]
        except KeyError:
            sem = threading.Semaphore()
            _semaphores[name] = sem

    return sem


@contextlib.contextmanager
def lock(name, lock_file_prefix=None, external=False, lock_path=None,
         do_log=True):
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
    """
    int_lock = internal_lock(name)
    with int_lock:
        if do_log:
            LOG.debug('Acquired semaphore "%(lock)s"', {'lock': name})
        try:
            if external and not CONF.oslo_concurrency.disable_process_locking:
                ext_lock = external_lock(name, lock_file_prefix, lock_path)
                with ext_lock:
                    yield ext_lock
            else:
                yield int_lock
        finally:
            if do_log:
                LOG.debug('Releasing semaphore "%(lock)s"', {'lock': name})


def synchronized(name, lock_file_prefix=None, external=False, lock_path=None):
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
                          do_log=False):
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
    oslo-concurrency-lock-wrapper

    If you run this:
        oslo-concurrency-lock-wrapper python setup.py testr <etc>

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
