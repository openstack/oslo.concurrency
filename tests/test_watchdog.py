# Copyright (c) 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging
import subprocess
import time

import fixtures
from oslotest import base as test_base

from oslo_concurrency import watchdog

LOG_FORMAT = '%(levelname)s %(message)s'


class WatchdogTest(test_base.BaseTestCase):
    def setUp(self):
        super(WatchdogTest, self).setUp()
        # capture the log bits where we can interrogate them
        self.logger = logging.getLogger()
        self.logger.setLevel(logging.DEBUG)
        self.log = self.useFixture(
            fixtures.FakeLogger(format=LOG_FORMAT, level=None)
        )

    def test_in_process_delay(self):
        with watchdog.watch(self.logger, "in process", after=1.0):
            time.sleep(2)
        self.assertIn("DEBUG in process not completed after 1",
                      self.log.output)
        loglines = self.log.output.rstrip().split("\n")
        self.assertEqual(1, len(loglines), loglines)

    def test_level_setting(self):
        with watchdog.watch(self.logger, "in process",
                            level=logging.ERROR, after=1.0):
            time.sleep(2)
        self.assertIn("ERROR in process not completed after 1",
                      self.log.output)
        loglines = self.log.output.rstrip().split("\n")
        self.assertEqual(1, len(loglines), loglines)

    def test_in_process_delay_no_message(self):
        with watchdog.watch(self.logger, "in process", after=1.0):
            pass
        # wait long enough to know there won't be a message emitted
        time.sleep(2)
        self.assertEqual('', self.log.output)

    def test_in_process_exploding(self):
        try:
            with watchdog.watch(self.logger, "ungraceful exit", after=1.0):
                raise Exception()
        except Exception:
            pass
        # wait long enough to know there won't be a message emitted
        time.sleep(2)
        self.assertEqual('', self.log.output)

    def test_subprocess_delay(self):
        with watchdog.watch(self.logger, "subprocess", after=0.1):
            subprocess.call("sleep 2", shell=True)
        self.assertIn("DEBUG subprocess not completed after 0",
                      self.log.output)
