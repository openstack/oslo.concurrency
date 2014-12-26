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

from oslotest import base as test_base

# Do NOT change this to new namespace, it's testing the old namespace
# passing hacking. Do NOT add a #noqa line, the point is this has to
# pass without it.
from oslo.concurrency import lockutils


class ImportTestCase(test_base.BaseTestCase):
    """Test that lockutils can be imported from old namespace.

    This also ensures that hacking rules on this kind of import will
    work for the rest of OpenStack.

    """

    def test_imported(self):
        self.assertEqual(len(lockutils._opts), 2,
                         "Lockutils.opts: %s" % lockutils._opts)
