#!/usr/bin/env python

#Copyright (C) 2009-2011 by Benedict Paten (benedictpaten@gmail.com)
#
#Released under the MIT license, see LICENSE.txt
#!/usr/bin/env python

"""Script for running an all against all (including self) set of alignments on a set of input
sequences. Uses the jobTree framework to parallelise the blasts.
"""
import os
import sys
import math
import errno
from optparse import OptionParser
from bz2 import BZ2File
import copy
import xml.etree.ElementTree as ET

from sonLib.bioio import logger
from sonLib.bioio import system, popenCatch, popenPush
from sonLib.bioio import getLogLevelString
from sonLib.bioio import newickTreeParser
from sonLib.bioio import makeSubDir
from sonLib.bioio import catFiles, getTempFile
from jobTree.scriptTree.target import Target
from jobTree.scriptTree.stack import Stack
from cactus.shared.common import getOptionalAttrib, runCactusAnalyseAssembly
from sonLib.bioio import setLoggingFromOptions
from cactus.shared.configWrapper import ConfigWrapper

class PreprocessorOptions:
    def __init__(self, chunkSize, cmdLine, memory, cpu, check, proportionToSample, unmask):
        self.chunkSize = chunkSize
        self.cmdLine = cmdLine
        self.memory = memory
        self.cpu = cpu
        self.check = check
        self.proportionToSample=proportionToSample
        self.unmask = unmask

class PreprocessChunk(Target):
    """ locally preprocess a fasta chunk, output then copied back to input
    """
    def __init__(self, prepOptions, seqPaths, proportionSampled, inChunk, outChunk):
        Target.__init__(self, memory=prepOptions.memory, cpu=prepOptions.cpu)
        self.prepOptions = prepOptions 
        self.seqPaths = seqPaths
        self.inChunk = inChunk
        self.outChunk = outChunk
        self.proportionSampled = proportionSampled
    
    def run(self):
        cmdline = self.prepOptions.cmdLine.replace("IN_FILE", "\"" + self.inChunk + "\"")
        cmdline = cmdline.replace("OUT_FILE", "\"" + self.outChunk + "\"")
        cmdline = cmdline.replace("TEMP_DIR", "\"" + self.getLocalTempDir() + "\"")
        cmdline = cmdline.replace("PROPORTION_SAMPLED", str(self.proportionSampled))
        logger.info("Preprocessor exec " + cmdline)
        #print "command", cmdline
        #sys.exit(1)
        popenPush(cmdline, " ".join(self.seqPaths))
        if self.prepOptions.check:
            system("cp %s %s" % (self.inChunk, self.outChunk))

class MergeChunks(Target):
    """ merge a list of chunks into a fasta file
    """
    def __init__(self, prepOptions, chunkList, outSequencePath):
        Target.__init__(self, cpu=prepOptions.cpu)
        self.prepOptions = prepOptions 
        self.chunkList = chunkList
        self.outSequencePath = outSequencePath
    
    def run(self):
        popenPush("cactus_batch_mergeChunks > %s" % self.outSequencePath, " ".join(self.chunkList))
 
class PreprocessSequence(Target):
    """Cut a sequence into chunks, process, then merge
    """
    def __init__(self, prepOptions, inSequencePath, outSequencePath):
        Target.__init__(self, cpu=prepOptions.cpu)
        self.prepOptions = prepOptions 
        self.inSequencePath = inSequencePath
        self.outSequencePath = outSequencePath
    
    def run(self):        
        logger.info("Preparing sequence for preprocessing")
        # chunk it up
        inChunkDirectory = makeSubDir(os.path.join(self.getGlobalTempDir(), "preprocessChunksIn"))
        inChunkList = [ chunk for chunk in popenCatch("cactus_blast_chunkSequences %s %i 0 %s %s" % \
               (getLogLevelString(), self.prepOptions.chunkSize,
                inChunkDirectory, self.inSequencePath)).split("\n") if chunk != "" ]   
        outChunkDirectory = makeSubDir(os.path.join(self.getGlobalTempDir(), "preprocessChunksOut"))
        outChunkList = [] 
        #For each input chunk we create an output chunk, it is the output chunks that get concatenated together.
        for i in xrange(len(inChunkList)):
            outChunkList.append(os.path.join(outChunkDirectory, "chunk_%i" % i))
            #Calculate the number of chunks to use
            inChunkNumber = int(max(1, math.ceil(len(inChunkList) * self.prepOptions.proportionToSample)))
            assert inChunkNumber <= len(inChunkList) and inChunkNumber > 0
            #Now get the list of chunks flanking and including the current chunk
            j = max(0, i - inChunkNumber/2)
            inChunks = inChunkList[j:j+inChunkNumber]
            if len(inChunks) < inChunkNumber: #This logic is like making the list circular
                inChunks += inChunkList[:inChunkNumber-len(inChunks)]
            assert len(inChunks) == inChunkNumber
            self.addChildTarget(PreprocessChunk(self.prepOptions, inChunks, float(inChunkNumber)/len(inChunkList), inChunkList[i], outChunkList[i]))
        # follow on to merge chunks
        self.setFollowOnTarget(MergeChunks(self.prepOptions, outChunkList, self.outSequencePath))

def unmaskFasta(inFasta, outFasta):
    """Uppercase a fasta file (removing the soft-masking)."""
    with open(outFasta, 'w') as out:
        for line in open(inFasta):
            if len(line) > 0 and line[0] != ">":
                out.write(line.upper())
            else:
                out.write(line)

class BatchPreprocessor(Target):
    def __init__(self, prepXmlElems, inSequence, 
                 globalOutSequence, iteration = 0):
        Target.__init__(self, time=0.0002) 
        self.prepXmlElems = prepXmlElems
        self.inSequence = inSequence
        self.globalOutSequence = globalOutSequence
        prepNode = self.prepXmlElems[iteration]
        self.memory = getOptionalAttrib(prepNode, "memory", typeFn=int, default=sys.maxint)
        self.cpu = getOptionalAttrib(prepNode, "cpu", typeFn=int, default=sys.maxint)
        self.iteration = iteration
              
    def run(self):
        # Parse the "preprocessor" config xml element     
        assert self.iteration < len(self.prepXmlElems)
        
        prepNode = self.prepXmlElems[self.iteration]
        prepOptions = PreprocessorOptions(int(prepNode.get("chunkSize", default="-1")),
                                          prepNode.attrib["preprocessorString"],
                                          int(self.memory),
                                          int(self.cpu),
                                          bool(int(prepNode.get("check", default="0"))),
                                          getOptionalAttrib(prepNode, "proportionToSample", typeFn=float, default=1.0),
                                          getOptionalAttrib(prepNode, "unmask", typeFn=bool, default=False))
        
        #output to temporary directory unless we are on the last iteration
        lastIteration = self.iteration == len(self.prepXmlElems) - 1
        if lastIteration == False:
            outSeq = os.path.join(self.getGlobalTempDir(), str(self.iteration))
        else:
            outSeq = self.globalOutSequence

        if prepOptions.unmask:
            unmaskedInputFile = getTempFile(rootDir=self.getGlobalTempDir())
            unmaskFasta(self.inSequence, unmaskedInputFile)
            self.inSequence = unmaskedInputFile

        if prepOptions.chunkSize <= 0: #In this first case we don't need to break up the sequence
            self.addChildTarget(PreprocessChunk(prepOptions, [ self.inSequence ], 1.0, self.inSequence, outSeq))
        else:
            self.addChildTarget(PreprocessSequence(prepOptions, self.inSequence, outSeq)) 
        
        if lastIteration == False:
            self.setFollowOnTarget(BatchPreprocessor(self.prepXmlElems, outSeq,
                                                     self.globalOutSequence, self.iteration + 1))
        else:
            self.setFollowOnTarget(BatchPreprocessorEnd(self.globalOutSequence))

class BatchPreprocessorEnd(Target):
    def __init__(self,  globalOutSequence):
        Target.__init__(self) 
        self.globalOutSequence = globalOutSequence
        
    def run(self):
        analysisString = runCactusAnalyseAssembly(self.globalOutSequence)
        self.logToMaster("After preprocessing assembly we got the following stats: %s" % analysisString)

############################################################
############################################################
############################################################
##The preprocessor phase, which modifies the input sequences
############################################################
############################################################
############################################################

class CactusPreprocessor(Target):
    """Modifies the input genomes, doing things like masking/checking, etc.
    """
    def __init__(self, inputSequences, outputSequences, configNode):
        Target.__init__(self)
        self.inputSequences = inputSequences
        self.outputSequences = outputSequences
        assert len(self.inputSequences) == len(self.outputSequences) #If these are not the same length then we have a problem
        self.configNode = configNode  
    
    def run(self):
        for inputSequenceFileOrDirectory, outputSequenceFile in zip(self.inputSequences, self.outputSequences):
            if not os.path.isfile(outputSequenceFile): #Only create the output sequence if it doesn't already exist. This prevents reprocessing if the sequence is used in multiple places between runs.
                self.addChildTarget(CactusPreprocessor2(inputSequenceFileOrDirectory, outputSequenceFile, self.configNode))
  
    @staticmethod
    def getOutputSequenceFiles(inputSequences, outputSequenceDir):
        """Function to get unambiguous file names for each input sequence in the output sequence dir. 
        """
        if not os.path.isdir(outputSequenceDir):
            os.mkdir(outputSequenceDir)
        return [ os.path.join(outputSequenceDir, inputSequences[i].split("/")[-1] + "_%i" % i) for i in xrange(len(inputSequences)) ]
        #return [ os.path.join(outputSequenceDir, "_".join(inputSequence.split("/"))) for inputSequence in inputSequences ]
  
class CactusPreprocessor2(Target):
    def __init__(self, inputSequenceFileOrDirectory, outputSequenceFile, configNode):
        Target.__init__(self)
        self.inputSequenceFileOrDirectory = inputSequenceFileOrDirectory
        self.outputSequenceFile = outputSequenceFile
        self.configNode = configNode
        
    def run(self):
        #If the files are in a sub-dir then rip them out.
        if os.path.isdir(self.inputSequenceFileOrDirectory):
            tempFile = getTempFile(rootDir=self.getGlobalTempDir())
            catFiles([ os.path.join(self.inputSequenceFileOrDirectory, f) for f in os.listdir(self.inputSequenceFileOrDirectory)], tempFile)
            inputSequenceFile = tempFile
        else:
            inputSequenceFile = self.inputSequenceFileOrDirectory
            
        assert inputSequenceFile != self.outputSequenceFile
        
        prepXmlElems = self.configNode.findall("preprocessor")
        
        analysisString = runCactusAnalyseAssembly(inputSequenceFile)
        self.logToMaster("Before running any preprocessing on the assembly: %s got following stats (assembly may be listed as temp file if input sequences from a directory): %s" % \
                         (self.inputSequenceFileOrDirectory, analysisString))
        
        if len(prepXmlElems) == 0: #Just cp the file to the output file
            system("cp %s %s" % (inputSequenceFile, self.outputSequenceFile))
        else:
            logger.info("Adding child batch_preprocessor target")
            self.addChildTarget(BatchPreprocessor(prepXmlElems, inputSequenceFile, self.outputSequenceFile, 0))
                    
def main():
    usage = "usage: %prog outputSequenceDir configXMLFile inputSequenceFastaFilesxN [options]"
    parser = OptionParser(usage=usage)
    Stack.addJobTreeOptions(parser) 
    
    options, args = parser.parse_args()
    setLoggingFromOptions(options)
    
    if len(args) < 3:
        raise RuntimeError("Too few input arguments: %s" % " ".join(args))
    
    outputSequenceDir = args[0]
    configFile = args[1]
    inputSequences = args[2:]
    
    #Replace any constants
    configNode = ET.parse(configFile).getroot()
    if configNode.find("constants") != None:
        ConfigWrapper(configNode).substituteAllPredefinedConstantsWithLiterals()
    
    Stack(CactusPreprocessor(inputSequences, CactusPreprocessor.getOutputSequenceFiles(inputSequences, outputSequenceDir), configNode)).startJobTree(options)

def _test():
    import doctest      
    return doctest.testmod()

if __name__ == '__main__':
    from cactus.preprocessor.cactus_preprocessor import *
    main()
