=====================
 Administrator Guide
=====================

This section contains information useful to administrators operating a service
that uses oslo.concurrency.

Lock File Management
====================

For services that use oslo.concurrency's external lock functionality for
interprocess locking, lock files will be stored in the location specified
by the ``lock_path`` config option in the ``oslo_concurrency`` section.
These lock files are not automatically deleted by oslo.concurrency because
the library has no way to know when the service is done with a lock, and
deleting a lock file that is being held by the service would cause
concurrency problems. Some services do delete lock files when they are done
with them, but deletion of a service's lock files while the service is
running should only be done by the service itself. External cleanup methods
cannot reasonably know when a lock is no longer needed.

However, to prevent the ``lock_path`` directory from growing indefinitely,
it is a good idea to occasionally delete all the lock files from it. The only
safe time to do this is when the service is not running, such as after a
reboot or when the service is down for maintenance. In the latter case, make
sure that all related services (such as api, worker, conductor, etc) are down
If any process that might hold locks is still running, deleting lock files may
introduce inconsistency in the service. One possible approach to this cleanup
is to put the ``lock_path`` in tmpfs so it will be automatically cleared on
reboot.

Note that in general, leftover lock files are a cosmetic nuisance at worst.
If you do run into a functional problem as a result of large numbers of
lock files, please report it to the Oslo team so we can look into other
mitigation strategies.

Frequently Asked Questions
==========================

What is the history of the lock file issue?
-------------------------------------------

It comes up every few months when a deployer of OpenStack notices that they
have a large number of lock files lying around, apparently unused. A thread
is started on the mailing list and one of the Oslo developers has to provide
an explanation of why it works the way it does. This FAQ is intended to be an
official replacement for the one-off explanation that is usually given.

The code responsible for this behavior has actually moved to the
`fasteners <https://github.com/harlowja/fasteners>`_ project, and there
is an
`issue addressing the leftover lock files <https://github.com/harlowja/fasteners/issues/26>`_
there. It covers much of the technical history of the problem, as well as
some proposed solutions.

Why hasn't this been fixed yet?
-------------------------------

Because to the Oslo developers' knowledge, no one has ever had a functional
issue as a result of leftover lock files. This makes it a lower priority
problem, and because of the complexity of fixing it nobody has been able to
yet. If functional issues are found, they should be reported as a bug
against oslo.concurrency so they can be tracked. In the meantime, this will
likely continue to be treated as a cosmetic annoyance and prioritized
appropriately.

Why aren't lock files deleted when the lock is released?
--------------------------------------------------------

In our testing, when a lock file was deleted while another process was waiting
for it, it created a sort of split-brain situation between any process that had
been waiting for the deleted file, and any process that attempted to lock the
file after it had been deleted. Essentially, two processes could end up holding
the same lock at the same time, which made this an unacceptable solution.

Why don't you use some other method of interprocess locking?
------------------------------------------------------------

We tried. Both Posix and SysV IPC were explored as alternatives. Unfortunately,
both have significant issues on Linux. Posix locks cannot be broken if the
process holding them crashes (at least not without a reboot). SysV locks have
a limited range of numerical ids, and because oslo.concurrency supports
string-based lock names, the possibility of collisions when hashing names was
too high. It was deemed better to have the file-based locking mechanism that
would always work than a different method that introduced serious new problems.

Bonus Question: Why doesn't ``lock_path`` default to a temp directory?
----------------------------------------------------------------------

Because every process that may need to hold a lock must use the same value
for ``lock_path`` or it becomes useless. If we allowed ``lock_path`` to be
unset and just created a temp directory on startup, each process would create
its own temp directory and there would be no actual coordination between them.

While this isn't strictly related to the lock file issue, it is another FAQ
about oslo.concurrency so it made sense to mention it here.
