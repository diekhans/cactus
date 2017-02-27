#!/usr/bin/env python

#Copyright (C) 2013 by Glenn Hickey
#
#Released under the MIT license, see LICENSE.txt

import os
import sys
import random
import math
import xml.etree.ElementTree as ET

from toil.lib.bioio import getTempFile, logger
from toil.job import Job
from cactus.shared.experimentWrapper import DbElemWrapper
from cactus.shared.experimentWrapper import ExperimentWrapper
from cactus.pipeline.ktserverControl import runKtserver
from cactus.pipeline.ktserverControl import blockUntilKtserverIsRunning
from cactus.pipeline.ktserverControl import killKtServer
from cactus.pipeline.ktserverControl import getKtServerReport
from cactus.pipeline.ktserverControl import ktServerAlreadyRunning
from cactus.pipeline.ktserverControl import getHostName
from cactus.pipeline.ktserverControl import getLogPath


class KtServerService(Job.Service):
    def __init__(self, dbElem, isSecondary, memory=None, cores=None, disk = None):
        Job.Service.__init__(self, memory=memory, cores=cores, disk=disk, preemptable=False)
        self.dbElem = dbElem
        self.isSecondary = isSecondary
        self.blockTimestep = 10
        self.blockTimeout = sys.maxint
        self.killSwitchPath = None
        self.process = None

    def start(self, job):
        if self.isSecondary == False:
            self.dbElem.setDbDir(os.path.join(job.fileStore.getLocalTempDir(), "cactusDB"))
        else:
            self.dbElem.setDbDir(os.path.join(job.fileStore.getLocalTempDir(), "tempDB/"))

        if not os.path.exists(self.dbElem.getDbDir()):
            os.mkdir(self.dbElem.getDbDir())
        self.killSwitchPath = getTempFile(suffix="_kill.txt",
                                          rootDir=self.dbElem.getDbDir())
        killSwitchFile = open(self.killSwitchPath, "w")
        killSwitchFile.write("init")
        killSwitchFile.close()


        self.process = runKtserver(job, self.dbElem, self.killSwitchPath, fileStore = job.fileStore)
        assert self.dbElem.getDbHost() != None
        
        #blockUntilKtserverIsRunning(job, self.dbElem, self.killSwitchPath, self.blockTimeout, self.blockTimestep)
        return self.dbElem.getConfString()
        

    def stop(self, job):
        logPath = getLogPath(self.dbElem)
        if os.path.exists(logPath):
            os.remove(logPath)
        if self.killSwitchPath:
            os.remove(self.killSwitchPath)
    def check(self):
        return True
    
