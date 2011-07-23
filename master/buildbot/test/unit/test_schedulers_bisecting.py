# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from twisted.internet import defer
from twisted.trial import unittest
from buildbot.schedulers import base, bisecting
from buildbot.status.results import SUCCESS, WARNINGS, FAILURE, EXCEPTION
from buildbot.test.util import scheduler
from buildbot.test.fake import fakedb
from twisted.internet.defer import Deferred

class BisectScheduler(scheduler.SchedulerMixin, unittest.TestCase):

    SCHEDULERID = 36
    UPSTREAM_NAME = 'uppy'

    def setUp(self):
        self.setUpScheduler()
        self.changeid = 1

    def tearDown(self):
        self.tearDownScheduler()

    def makeScheduler(self, **kwargs):
        # build a fake upstream scheduler
        class Upstream(base.BaseScheduler):
            def __init__(self, name, builderNames):
                self.name = name
                self.builderNames = builderNames
        upstream = Upstream(self.UPSTREAM_NAME, builderNames=['a', 'b'])

        # by default, BisectScheduler builds on upstream builders
        sched = bisecting.BisectScheduler(name='n', upstream=upstream, **kwargs)
        self.attachScheduler(sched, self.SCHEDULERID)
        return sched

    # tests

    def test_constructor_builderNames_default(self):
        sched = self.makeScheduler()
        self.assertEqual(sched.builderNames, ['a', 'b'])

    def test_constructor_builderNames_not_default(self):
        sched = self.makeScheduler(builderNames=['a'])
        self.assertEqual(sched.builderNames, ['a'])

    def test_startService(self):
        sched = self.makeScheduler()
        sched.startService()

        callbacks = self.master.getSubscriptionCallbacks()
        self.assertNotEqual(callbacks['buildsets'], None)
        self.assertNotEqual(callbacks['buildset_completion'], None)

        d = sched.stopService()
        def check(_):
            callbacks = self.master.getSubscriptionCallbacks()
            self.assertEqual(callbacks['buildsets'], None)
            self.assertEqual(callbacks['buildset_completion'], None)
        d.addCallback(check)
        return d

    def test_load_state(self):
        sched = self.makeScheduler()
        self.db.schedulers.fakeState(self.SCHEDULERID,
            {"last_successful_changeid": 12,
             "builder_names": ['z'],
             "failing_changeids": [1,2,3],
             "pending_changeids": [4,5,6]})
        sched.startService()

        self.assertEqual(sched.last_successful_changeid, 12)
        self.assertEqual(sched.test_builderNames, ['z'])
        self.assertEqual(sched.failing_changeids, set([1,2,3]))
        self.assertEqual(sched.pending_changeids, set([4,5,6]))

    def test_save_state(self):
        sched = self.makeScheduler()
        sched.startService()

        sched.last_successful_changeid = 12
        sched.test_builderNames = ['z']
        sched.failing_changeids = set([1,2,3])
        sched.pending_changeids = set([4,5,6])
        sched._saveState()

        self.db.schedulers.assertState(self.SCHEDULERID,
            {"last_successful_changeid": 12,
             "builder_names": ['z'],
             "failing_changeids": [1,2,3],
             "pending_changeids": [4,5,6]})

    def fakeBuildset(self, bsid, numberOfChanges, schedulerName = None):
        ssid = bsid + 100
        changeids = range(self.changeid, self.changeid + numberOfChanges)
        self.changeid += numberOfChanges

        b = [fakedb.Buildset(id=bsid, sourcestampid=ssid)]
        br = [fakedb.BuildRequest(id=2*bsid,   buildsetid=bsid, buildername='a'),
              fakedb.BuildRequest(id=2*bsid+1, buildsetid=bsid, buildername='b'),]
        ss = [fakedb.SourceStamp(id=ssid)]
        c = [fakedb.Change(changeid=id) for id in changeids]
        ssc = [fakedb.SourceStampChange(sourcestampid=ssid, changeid=id) for id in changeids]

        self.db.insertTestData(b + br + ss + c + ssc)
        self.fakeBuildsetAddition(bsid, schedulerName)

    def fakeBuildsetAddition(self, bsid, schedulerName = None):
        callbacks = self.master.getSubscriptionCallbacks()
        callbacks['buildsets'](bsid=bsid,
                properties=dict(scheduler=(schedulerName or self.UPSTREAM_NAME, 'Scheduler')))

    @defer.deferredGenerator
    def fakeBuildsetCompletion(self, bsid, result):
        wfd = defer.waitForDeferred(
            self.db.buildrequests.getBuildRequests(bsid=bsid))
        yield wfd
        buildrequests = wfd.getResult()

        # To test a more complex situation, we give the first build request
        # the requested result, while we let all other build requests succeed.
        # This helps us test that the scheduler builds only on the builders
        # that failed in the first place, and not on any other builders.
        self.db.buildrequests.fakeBuildRequestCompletion(buildrequests[0]['brid'], result)
        for br in buildrequests[1:]:
            self.db.buildrequests.fakeBuildRequestCompletion(br['brid'], SUCCESS)

        self.db.buildsets.fakeBuildsetCompletion(bsid=bsid, result=result)

        callbacks = self.master.getSubscriptionCallbacks()
        callbacks['buildset_completion'](bsid, result)

    def _do_test(self, scheduler_name, expect_subscription, result):
        sched = self.makeScheduler()
        sched.startService()

        # pretend we saw a buildset with a matching name
        self.fakeBuildset(44, 0, scheduler_name)

        # check whether scheduler is subscribed to that buildset
        if expect_subscription:
            self.db.buildsets.assertBuildsetSubscriptions((self.SCHEDULERID, 44))
        else:
            self.db.buildsets.assertBuildsetSubscriptions()

        # pretend that the buildset is finished
        self.fakeBuildsetCompletion(44, result)

        self.db.buildsets.assertBuildsets(1) # only the one we added above

    def do_test(self, result):
        return self._do_test(self.UPSTREAM_NAME, True, result)

    # neither of these should kick off a build

    def test_unrelated_buildset(self):
        return self._do_test('unrelated', False, SUCCESS)

    def test_SUCCESS(self):
        return self.do_test(SUCCESS)

    def test_WARNINGS(self):
        return self.do_test(WARNINGS)

    def test_FAILURE(self):
        return self.do_test(FAILURE)

    def do_setup(self, green_changes, red_changes):
        sched = self.makeScheduler()
        sched.startService()

        self.fakeBuildset(44, green_changes)
        self.fakeBuildset(45, red_changes)

        self.fakeBuildsetCompletion(44, SUCCESS)
        self.fakeBuildsetCompletion(45, FAILURE)

        return sched

    def test_buildset_without_changes_should_not_count_as_success(self):
        self.do_setup(0, 2)
        self.db.buildsets.assertBuildsets(2)

    def test_failed_buildset_1_change(self):
        self.do_setup(1, 1)
        self.db.buildsets.assertBuildsets(2)

    # these should kick off one build

    def assertBuildset(self, changeids, remove_bsids = []):
        bsids = self.db.buildsets.allBuildsetIds()
        bsids.remove(44) # see do_setup
        bsids.remove(45)

        for bsid in remove_bsids:
            bsids.remove(bsid)

        self.db.buildsets.assertBuildset(bsids[0],
                dict(external_idstring=None,
                     properties=[('scheduler', ('n', 'Scheduler'))],
                     reason='bisecting'),
                dict(revision='abcd', branch='master',
                     project='proj', repository='repo',
                     changeids=set(changeids)))

        return bsids[0]

    @defer.deferredGenerator
    def assertBuildrequests(self, bsid, expected_count):
        wfd = defer.waitForDeferred(
            self.db.buildrequests.getBuildRequests(bsid=bsid))
        yield wfd
        buildrequests = wfd.getResult()
        self.assertEqual(len(buildrequests), expected_count)

    def prepare_test_two(self, red_changes, result):
        sched = self.do_setup(1, red_changes)

        self.db.buildsets.assertBuildsets(3)
        bsid = self.assertBuildset([2])

        self.fakeBuildsetAddition(bsid, sched.name)
        self.fakeBuildsetCompletion(bsid, result)

        self.assertBuildrequests(bsid, 1)

        return sched, bsid

    def do_test_two(self, result, expected_guilty):
        sched, _ = self.prepare_test_two(2, result)

        self.db.buildsets.assertBuildsets(3)
        self.assertEqual(sched.guilty, expected_guilty)

        return sched

    def test_failed_buildset_2_changes_triggee_succeeds(self):
        self.do_test_two(SUCCESS, 3)

    def test_failed_buildset_2_changes_triggee_fails(self):
        self.do_test_two(FAILURE, 2)

    def test_failed_buildset_2_changes_triggee_exception(self):
        sched = self.do_test_two(EXCEPTION, None)
        self.assertEqual(sched.failing_changeids, set())
        self.assertEqual(sched.pending_changeids, set())

    # these should kick off two builds

    def prepare_test_three(self, red_changes, result1, result2):
        sched, bsid = self.prepare_test_two(red_changes, result1)

        self.db.buildsets.assertBuildsets(4)
        bsid = self.assertBuildset([3], [bsid])

        self.fakeBuildsetAddition(bsid, sched.name)
        self.fakeBuildsetCompletion(bsid, result2)

        return sched

    def do_test_three(self, result1, result2, expected_guilty):
        sched = self.prepare_test_three(3, result1, result2)

        self.db.buildsets.assertBuildsets(4)
        self.assertEqual(sched.guilty, expected_guilty)

        return sched

    # Following tests are best explained using a table:
    # R = red (success), G = green (failure)
    # change 1 is green per definition
    # change 4 is red per definition
    #
    # changes
    # 1 2 3 4
    # G G G R  [2]: SUCCESS  [3]: SUCCESS  =>  guilty: 4
    # G G R R  [2]: SUCCESS  [3]: FAILURE  =>  guilty: 3
    # G R R R  [2]: FAILURE  [3]: FAILURE  =>  guilty: 2
    # G R G R  [2]: FAILURE  [3]: SUCCESS  =>  guilty: 4

    def test_failed_buildset_3_changes_triggee_succeeds(self):
        self.do_test_three(SUCCESS, SUCCESS, 4)

    def test_failed_buildset_3_changes_triggee_succeeds_fails(self):
        self.do_test_three(SUCCESS, FAILURE, 3)

    def test_failed_buildset_3_changes_triggee_fails(self):
        self.do_test_three(FAILURE, FAILURE, 2)

    def test_failed_buildset_3_changes_triggee_fails_succeeds(self):
        self.do_test_three(FAILURE, SUCCESS, 4)

    def test_failed_buildset_3_changes_triggee_fails_exception(self):
        sched = self.do_test_three(FAILURE, EXCEPTION, None)
        self.assertEqual(sched.failing_changeids, set())
        self.assertEqual(sched.pending_changeids, set())
