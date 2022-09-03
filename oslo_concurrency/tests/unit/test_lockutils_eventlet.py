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

import os
import tempfile

import eventlet
from eventlet import greenpool
from oslotest import base as test_base

from oslo_concurrency import lockutils


class TestFileLocks(test_base.BaseTestCase):

    def test_concurrent_green_lock_succeeds(self):
        """Verify spawn_n greenthreads with two locks run concurrently."""
        tmpdir = tempfile.mkdtemp()
        self.completed = False

        def locka(wait):
            a = lockutils.InterProcessLock(os.path.join(tmpdir, 'a'))
            with a:
                wait.wait()
            self.completed = True

        def lockb(wait):
            b = lockutils.InterProcessLock(os.path.join(tmpdir, 'b'))
            with b:
                wait.wait()

        wait1 = eventlet.event.Event()
        wait2 = eventlet.event.Event()
        pool = greenpool.GreenPool()
        pool.spawn_n(locka, wait1)
        pool.spawn_n(lockb, wait2)
        wait2.send()
        eventlet.sleep(0)
        wait1.send()
        pool.waitall()

        self.assertTrue(self.completed)


class TestInternalLock(test_base.BaseTestCase):
    def _test_internal_lock_with_two_threads(self, fair, spawn):
        self.other_started = eventlet.event.Event()
        self.other_finished = eventlet.event.Event()

        def other():
            self.other_started.send('started')
            with lockutils.lock("my-lock", fair=fair):
                pass
            self.other_finished.send('finished')

        with lockutils.lock("my-lock", fair=fair):
            # holding the lock and starting another thread that also wants to
            # take it before finishes
            spawn(other)
            # let the other thread start
            self.other_started.wait()
            eventlet.sleep(0)
            # the other thread should not have finished as it would need the
            # lock we are holding
            self.assertIsNone(
                self.other_finished.wait(0.5),
                "Two threads was able to take the same lock",
            )

        # we released the lock, let the other thread take it and run to
        # completion
        result = self.other_finished.wait()
        self.assertEqual('finished', result)

    def test_lock_with_spawn(self):
        self._test_internal_lock_with_two_threads(
            fair=False, spawn=eventlet.spawn
        )

    def test_lock_with_spawn_n(self):
        self._test_internal_lock_with_two_threads(
            fair=False, spawn=eventlet.spawn_n
        )

    def test_fair_lock_with_spawn(self):
        self._test_internal_lock_with_two_threads(
            fair=True, spawn=eventlet.spawn
        )

    def test_fair_lock_with_spawn_n(self):
        self._test_internal_lock_with_two_threads(
            fair=True, spawn=eventlet.spawn_n
        )
