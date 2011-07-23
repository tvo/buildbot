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
from twisted.python import log
from buildbot import util
from buildbot.schedulers.dependent import Dependent
from buildbot.status.results import SUCCESS, WARNINGS, FAILURE

class BisectScheduler(Dependent):

    def __init__(self, name, upstream, builderNames=None, properties={}):
        if not builderNames:
            builderNames = upstream.listBuilderNames()
        Dependent.__init__(self, name, upstream, builderNames, properties)

        # persistent state
        self.last_successful_changeid = None

        # persistent state - only used during bisect operation
        self.test_builderNames = None
        self.pending_changeids = set()
        self.failing_changeids = set()

        # transient state
        self.guilty = None

    def startService(self):
        Dependent.startService(self)
        self._loadState()

    @util.deferredLocked('_subscription_lock')
    def _buildsetAdded(self, bsid=None, properties=None, **kwargs):
        # check if this was submitted by our upstream by checking the
        # scheduler property
        submitter = properties.get('scheduler', (None, None))[0]
        if submitter != self.upstream_name and submitter != self.name:
            return

        # record our interest in this buildset, both locally and in the
        # database
        d = self.master.db.buildsets.subscribeToBuildset(
                                        self.schedulerid, bsid)
        d.addErrback(log.err, 'while subscribing to buildset %d' % bsid)

    @util.deferredLocked('_subscription_lock')
    @defer.deferredGenerator
    def _checkCompletedBuildsets(self, bsid, result):
        wfd = defer.waitForDeferred(
            self.master.db.buildsets.getSubscribedBuildsets(self.schedulerid))
        yield wfd
        subs = wfd.getResult()

        for (sub_bsid, sub_ssid, sub_complete, sub_results) in subs:
            # skip incomplete builds, handling the case where the 'complete'
            # column has not been updated yet
            if not sub_complete and sub_bsid != bsid:
                continue

            # get changeids
            wfd = defer.waitForDeferred(
                self.master.db.sourcestamps.getSourceStamp(sub_ssid))
            yield wfd
            sourcestamp = wfd.getResult()
            changeids = sorted(sourcestamp['changeids'])

            if changeids:
                if sub_results in (SUCCESS, WARNINGS):
                    self._checkSucceededBuildset(sub_bsid, sub_ssid, changeids)
                elif sub_results in (FAILURE, ):
                    self._checkFailedBuildset(sub_bsid, sub_ssid, changeids)
                else:
                    self._checkExceptionBuildset(sub_bsid, sub_ssid, changeids)

            # and regardless of status, remove the subscription
            wfd = defer.waitForDeferred(
                self.master.db.buildsets.unsubscribeFromBuildset(
                                          self.schedulerid, sub_bsid))
            yield wfd
            wfd.getResult()

    def _checkSucceededBuildset(self, bsid, ssid, changeids):
        log.msg("%s '%s': _checkSucceededBuildset: %s" %
                (self.__class__.__name__, self.name, str(changeids)))

        self.last_successful_changeid = max(changeids)

        def purge_successful(changeids):
            return set(id for id in changeids if id > self.last_successful_changeid)

        self.pending_changeids = purge_successful(self.pending_changeids)
        self.failing_changeids = purge_successful(self.failing_changeids)

        self._continueSearching()

    @defer.deferredGenerator
    def _checkFailedBuildset(self, bsid, ssid, changeids):
        log.msg("%s '%s': _checkFailedBuildset: %s" %
                (self.__class__.__name__, self.name, str(changeids)))

        # do nothing if we don't have a green starting point
        if not self.last_successful_changeid:
            return

        # if we aren't bisecting, store the names of the failed builders
        if not self.failing_changeids and not self.pending_changeids:
            wfd = defer.waitForDeferred(
                self.master.db.buildrequests.getBuildRequests(bsid=bsid))
            yield wfd
            buildrequests = wfd.getResult()
            builderNames = set(br['buildername'] for br in buildrequests
                               if br['results'] == FAILURE)
            self.test_builderNames = list(
                builderNames.intersection(self.builderNames))

        # last change is known faulty now, we need to test the other changes
        self.failing_changeids.add(changeids[-1])
        self.pending_changeids.update(changeids[:-1])

        self._continueSearching()

    def _checkExceptionBuildset(self, bsid, ssid, changeids):
        log.msg("%s '%s': _checkExceptionBuildset: %s" %
                (self.__class__.__name__, self.name, str(changeids)))

        # stop bisecting
        self.failing_changeids = set()
        self.pending_changeids = set()
        self._saveState()

    def _continueSearching(self):
        # anything to do at all?
        if not self.pending_changeids and not self.failing_changeids:
            return

        log.msg("%s '%s': _continueSearching: pending %s failing %s" %
                (self.__class__.__name__, self.name,
                 str(list(self.pending_changeids)),
                 str(list(self.failing_changeids))))

        if len(self.pending_changeids) == 1:
            changeids = [self.pending_changeids.pop()]
            self._addBuildsetForChanges(changeids)

        elif len(self.pending_changeids) > 1:
            changeids = sorted(self.pending_changeids)
            changeids = changeids[:(len(changeids) + 1) / 2]
            for changeid in changeids:
                self.pending_changeids.remove(changeid)
            self._addBuildsetForChanges(changeids)

        else:
            self._onGuilty(min(self.failing_changeids))
            self.failing_changeids = set()

        self._saveState()

    def _addBuildsetForChanges(self, changeids):
        d = self.addBuildsetForChanges(reason='bisecting',
                changeids=changeids, builderNames=self.test_builderNames)
        d.addErrback(log.err, 'while adding buildset for %s on builders %s' %
                     (str(changeids), str(self.test_builderNames)))

    def _onGuilty(self, guilty):
        self.guilty = guilty
        log.msg("%s '%s': the guilty change was %d" %
                (self.__class__.__name__, self.name, guilty))

    def _saveState(self):
        state_dict = dict(
            last_successful_changeid = self.last_successful_changeid,
            builder_names = self.test_builderNames,
            pending_changeids = list(self.pending_changeids),
            failing_changeids = list(self.failing_changeids))

        d = self.master.db.schedulers.setState(self.schedulerid, state_dict)
        d.addErrback(log.err, "while saving state for %s '%s'" %
                (self.__class__.__name__, self.name))

    def _loadState(self):
        d = self.master.db.schedulers.getState(self.schedulerid)
        def setState(state_dict):
            self.last_successful_changeid = state_dict.get('last_successful_changeid')
            self.test_builderNames = state_dict.get('builder_names', [])
            self.pending_changeids = set(state_dict.get('pending_changeids', []))
            self.failing_changeids = set(state_dict.get('failing_changeids', []))
        d.addCallback(setState)
        d.addErrback(log.err, "while loading state for %s '%s'" %
                (self.__class__.__name__, self.name))
