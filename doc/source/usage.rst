=======
 Usage
=======

To use oslo.concurrency in a project, import the relevant module. For
example::

    from oslo_concurrency import lockutils
    from oslo_concurrency import processutils

.. seealso::

   * :doc:`API Documentation <api/index>`

Locking a function (local to a process)
=======================================

To ensure that a function (which is not thread safe) is only used in
a thread safe manner (typically such type of function should be refactored
to avoid this problem but if not then the following can help)::

    @lockutils.synchronized('not_thread_safe')
    def not_thread_safe():
        pass

Once decorated later callers of this function will be able to call into
this method and the contract that two threads will **not** enter this
function at the same time will be upheld. Make sure that the names of the
locks used are carefully chosen (typically by namespacing them to your
app so that other apps will not chose the same names).

Locking a function (local to a process as well as across process)
=================================================================

To ensure that a function (which is not thread safe **or** multi-process
safe) is only used in a safe manner (typically such type of function should
be refactored to avoid this problem but if not then the following can help)::

    @lockutils.synchronized('not_thread_process_safe', external=True)
    def not_thread_process_safe():
        pass

Once decorated later callers of this function will be able to call into
this method and the contract that two threads (or any two processes)
will **not** enter this function at the same time will be upheld. Make
sure that the names of the locks used are carefully chosen (typically by
namespacing them to your app so that other apps will not chose the same
names).

Common ways to prefix/namespace the synchronized decorator
==========================================================

Since it is **highly** recommended to prefix (or namespace) the usage
of the synchronized there are a few helpers that can make this much easier
to achieve.

An example is::

    myapp_synchronized = lockutils.synchronized_with_prefix("myapp")

Then further usage of the ``lockutils.synchronized`` would instead now use
this decorator created above instead of using ``lockutils.synchronized``
directly.

Command Line Wrapper
====================

``oslo.concurrency`` includes a command line tool for use in test jobs
that need the environment variable :envvar:`OSLO_LOCK_PATH` set. To
use it, prefix the command to be run with
:command:`lockutils-wrapper`. For example::

  $ lockutils-wrapper env | grep OSLO_LOCK_PATH
  OSLO_LOCK_PATH=/tmp/tmpbFHK45
