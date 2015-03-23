#!/usr/bin/env python

import time
import sys
import json
import os.path
import socket
import logging
import subprocess

from StringIO import StringIO
from kazoo.client import KazooClient
from threading import Thread, Event

class ZookeeperNode(object):

    def __init__(self, cfg, nodeCfg, index, hostname="localhost", timeout=1):
        self.cfg = cfg
        self.nodeCfg = nodeCfg
        self.index = index
        self.hostname = hostname
        self.timeout = timeout
        self.id = self.nodeCfg["id"]
        self.name = self.nodeCfg["name"]
        self.log = logging.getLogger(self.name)
        self.clientPort = self.cfg["basePort"] + (self.cfg["nodePortOffset"] * self.index)
        self.peerPort = self.clientPort + self.cfg["peerPortOffset"]
        self.leaderPort = self.clientPort + self.cfg["leaderPortOffset"]
        self.observer = self.nodeCfg.get("observer",False)
        self.cfgFilePath = os.path.join(self.cfg["nodesConfDir"], "node-%s.cfg" % self.name)
        self.dataDir = os.path.join(self.cfg["dataDir"], "node-%s" % self.name)
        self.logDir = os.path.join(self.cfg["logDir"], "node-%s" % self.name)
        self.addr = (self.hostname, self.clientPort)
        self.status = { "zk_server_state" : "none" }
        self.monitorThread = None
        self.monitorStop = Event()

        if not os.path.exists(self.dataDir):
            os.makedirs(self.dataDir)

        if not os.path.exists(self.logDir):
            os.makedirs(self.logDir)

    def __repr__(self):
        return "[%s id=%d port=%d]" % (self.name, self.id, self.clientPort)

    def init(self):
        self.setStatus(self.fetchStatus())
        self.monitorThread = Thread(target=self._statusLoop)
        self.monitorThread.daemon = True
        self.monitorThread.start()

    def close(self):
        self.monitorStop.set()
        self.monitorThread.join()

    def setStatus(self, status):
        oldStatus = self.status
        self.status = status

        if oldStatus != self.status:
            self.log.info("Server state change: %s -> %s" % (oldStatus["zk_server_state"], self.status["zk_server_state"]))

    def parseStatusLine(self,ln):
        if ln.strip().lower() == "this zookeeper instance is not currently serving requests":
            return ("zk_server_status","offline")

        (key, val) = ln.strip().split("\t")
        return (key, val)

    def createConfig(self):
        if not self in self.cfg["quorum"]:
            if os.path.exists(self.cfgFilePath):
               os.remove(self.cfgFilePath) 


        cfgContent = self.cfg["config_template"]

        cfgContent += "\n"
        cfgContent += "dataDir=%s\n" % self.dataDir
        cfgContent += "clientPort=%d\n" % self.clientPort

        for node in self.cfg["quorum"]:
            if node.observer:
                cfgContent += "server.%d=%s:%d:%d:observer\n" % (node.id, node.hostname, node.peerPort, node.leaderPort)
            else:
                cfgContent += "server.%d=%s:%d:%d\n" % (node.id, node.hostname, node.peerPort, node.leaderPort)

        fp = None
        try:
            fp = open(self.cfgFilePath,"w")
            fp.write(cfgContent)
        finally:
            fp.close()

    def start(self):
        if not self.checkStatus("down"):
            raise TesterException("Node %s is running, cannot start it..." % self)

        self.log.info("Starting %s" % self)
        out = StringIO()
        err = StringIO()
        rc = subprocess.call([self.cfg["zkCmd"], "start", self.cfgFilePath])
        if rc != 0:
            raise TesterException("Node %s failed to start..." % self)
        
    def stop(self):
        if self.checkStatus("down"):
            self.log.info("Node %s is down, no need to stop it" % self)
            return

        rc = subprocess.call([self.cfg["zkCmd"], "stop", self.cfgFilePath])
        raise TesterException("Node %s failed to stop..." % self)

    def checkStatus(self,expected):
        status = self.fetchStatus()
        if status["zk_server_state"] != expected:
            self.log.error("Expected state '%s' but state was '%s'" % (expected, status["zk_server_state"]))
            return False
        return True

    def _statusLoop(self):
        time.sleep(1)
        while not self.monitorStop.isSet():
            self.setStatus(self.fetchStatus())
            time.sleep(0.5)

    def fetchStatus(self):

        s = socket.socket()
        s.settimeout(self.timeout)

        try:

            s.connect(self.addr)
            s.send("mntr")
            data=[]
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                data.append(chunk)
           
            data = StringIO("".join(data))
        
            status = dict([ self.parseStatusLine(ln) for ln in data ])
        except socket.error as e:
            if e.errno == 111:
                status = { "zk_server_state" : "down" }
            else:
                raise e
        finally:
            s.close()

        return status

def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.DEBUG)
    log = logging.getLogger("main")

    if len(sys.argv) != 2:
        sys.stderr.write("Usage: %s <configFile>\n\n" % (sys.argv[0],))
        raise SystemExit(1)

    cfgFile = sys.argv[1]
    if not os.path.exists(cfgFile):
        sys.stderr.write("Could not find config file: %s\n\n" % cfgFile)
        raise SystemExit(1)

    # load JSON allowing for comment (# must be at beginning of line)
    cfg = json.loads("\n".join([ x for x in open(cfgFile) if x.lstrip()[:1] != "#"]))

    # add in additional config options
    cfg["baseDir"] = os.path.abspath(os.path.dirname(sys.argv[0]))
    cfg["zkDir"] = os.path.join(cfg["baseDir"],"zookeeper-3.4.6")
    cfg["confDir"] = os.path.join(cfg["baseDir"],"conf")
    cfg["nodesConfDir"] = os.path.join(cfg["confDir"],"nodes")
    cfg["dataDir"] = os.path.join(cfg["baseDir"],"data")
    cfg["logDir"] = os.path.join(cfg["baseDir"],"logs")
    cfg["zkBin"] = os.path.join(cfg["zkDir"],"bin")
    cfg["zkCmd"] = os.path.join(cfg["zkBin"],"zkServer.sh")

    cfg["quorum"] = []

    cfg["config_template"] = """
tickTime=%(tickTime)d
initLimit=%(initLimit)d
syncLimit=%(syncLimit)d
""".strip() % (cfg)

    nodes = [ZookeeperNode(cfg, cfg["nodes"][i], i) for i in range(0, len(cfg["nodes"]))]
    nodeMap = dict([ (n.name,n) for n in nodes])

    if not os.path.exists(cfg["nodesConfDir"]):
        os.makedirs(cfg["nodesConfDir"])

    if not os.path.exists(cfg["dataDir"]):
        os.makedirs(cfg["dataDir"])

    if not os.path.exists(cfg["logDir"]):
        os.makedirs(cfg["logDir"])

    try:
        for node in nodes:
            node.init()
       
        for action in cfg["actions"]:
            log.info("Processing action: %s" % action)
            actionName = action["action"]
            actionNodes = action.get("nodes")

            if actionName == "start":
                for node in [nodeMap[n] for n in actionNodes]:
                    node.start()

            elif actionName == "stop":
                for node in [nodeMap[n] for n in actionNodes]:
                    node.stop()

            elif actionName == "quorum":
                cfg["quorum"] = [nodeMap[n] for n in actionNodes]
                for node in cfg["quorum"]:
                    node.createConfig()

            else:
                raise TesterException("Unknown action: %s" % action)

        #time.sleep(5)

    finally:
        for node in nodes:
            try:
                node.close()
            except:
                log.exception("Failed to close node: %s" % node)

class TesterException(Exception):
    pass

if __name__ == "__main__":
    main()
