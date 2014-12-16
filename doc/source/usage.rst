=======
 Usage
=======

To use oslo.concurrency in a project, import the relevant module. For
example::

    from oslo_concurrency import lockutils

.. seealso::

   * :doc:`API Documentation <api/index>`

Command Line Wrapper
====================

``oslo.concurrency`` includes a command line tool for use in test jobs
that need the environment variable :envvar:`OSLO_LOCK_PATH` set. To
use it, prefix the command to be run with
:command:`lockutils-wrapper`. For example::

  $ lockutils-wrapper env | grep OSLO_LOCK_PATH
  OSLO_LOCK_PATH=/tmp/tmpbFHK45
