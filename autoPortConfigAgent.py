#!/usr/bin/env python3
# https://github.com/aristanetworks/EosSdk/blob/master/examples/IntfIpAddrMergeExample.py
#### http://aristanetworks.github.io/EosSdk/docs/2.19.0/ref/

import eossdk, yaml, json, sys, pyeapi, uuid, io, urllib.request, subprocess

class lldpCapsEnum:
    isOther = 0
    isRepeater = 1
    isBridge = 2
    isAP = 4
    isRouter = 8
    isTelephone = 16
    isDocsis = 32
    isStation = 64

def formatMac(mac):
    return mac.replace(':', '').replace('.','').replace('-', '').strip().lower()

# our monitor inherits from the
#  interface handler in order to subscribe to intf up/down events
#  the mac table handler in order to subscribe to mac learn events
class InterfaceMonitor(eossdk.AgentHandler, eossdk.IntfHandler, eossdk.MacTableHandler, eossdk.LldpHandler):
    def __init__(self, intfMgr, agentMgr, macMgr, lldpMgr):
        eossdk.AgentHandler.__init__(self, agentMgr)
        eossdk.IntfHandler.__init__(self, intfMgr)
        eossdk.MacTableHandler.__init__(self, macMgr)
        eossdk.LldpHandler.__init__(self, lldpMgr)
        self.tracer = eossdk.Tracer("autoPortConfigAgent")
        self.intfMgr_ = intfMgr
        self.agentMgr_ = agentMgr
        self.macTableMgr_ = macMgr
        self.lldpMgr = lldpMgr
        self.pyeapi = pyeapi.connect_to("localhost")
        # this list keeps track of which interfaces have received a linkup event
        #  and which we are still interested in mac events for as we have not yet
        #  learned anything to configure on.
        self.macInterfaces = []
        # this list keeps track of interfaces we are watching for lldp learns
        self.lldpInterfaces = []
        # this list keeps track of the interfaces we are currently configured to
        #  monitor for linkup/linkdown messages.
        self.monitoredInterfaces = []

        self.configs = {}
        self.vrf = None


    # the on_agent_option function is a standard callback called when an option is
    #  set in the configuration.  it can be called after agent startup if the user
    #  reconfigures.  it isn't called by default on startup so we manually call it
    #  on initial startup
    def on_agent_option(self, optionName, value):
        # if we have a new batch of interfaces to watch, let's figure them out
        if optionName == "interfaces":
            # turn off any monitoring that's already on
            self.tracer.trace5("Disabling all interface monitoring")
            self.watch_all_intfs(False)
            self.watch_all_mac_entries(False)
            self.macInterfaces = []
            self.lldpInterfaces = []
            self.monitoredInterfaces = []

            if value in ("", "all"):
                value = ""
                self.tracer.trace0("No specific interfaces have been set to be monitored!, monitoring everything")

            # we are wanting to monitor a new set of interfaces
            #  first, use pyeapi to pull the interface names
            #  of the requisite interfaces. "all" or "" will
            #  not limit the interfaces we are looking at
            t = []
            cmd = 'show int {} stat'.format(value)
            try:
                t = self.pyeapi.enable(cmd, autoComplete=True)
            except:
                self.tracer.trace0("Could not fetch the interface list properly.  Is management api configured?")
                pass

            if len(t) > 0:
                self.interfaces = t[0].get('result', []).get('interfaceStatuses',[])
                # loop over any interfaces that we get back from pyeapi
                #   and start the operstatus monitoring for each
                for intf in self.interfaces:
                    # grab a handle for this interface from eossdk
                    self.tracer.trace1("monitoring interface {}".format(intf))
                    self.watch_intf(eossdk.IntfId(intf), True)
                    self.monitoredInterfaces.append(intf)

        # we may need to use a vrf on the configuration
        elif optionName == "vrf":
            if value:
                self.vrf = value
            else:
                self.vrf = None

            # now we need to re-call the option for config and try to reparse
            configStr = self.agentMgr_.agent_option("config")
            self.on_agent_option("config", configStr)

        # order of precedence on config file type will be
        #   json formatted embedded string
        #   local file either json or yaml formatted
        #   remote file either json or yaml formatted
        # we'll break at the first success
        elif optionName == "config":
            # if the config option is being unset, we really want to noop that
            if value:
                # try the config as a string
                self.tracer.trace6(value)
                try:
                    self.tracer.trace0("Attempting embedded string")
                    configFile = io.StringIO(value)
                    self.configs = self.parseConfig(configFile)
                except:
                    pass
                else:
                    # there was no exception, it must have parsed. we're done here
                    self.tracer.trace0("Parsed an embedded string configuration")
                    return

                # try loading the local file
                try:
                    self.tracer.trace0("Attempting a local configuration file")
                    configFile = open(value, "r")
                    self.configs = self.parseConfig(configFile)
                except:
                    pass
                else:
                    # the file parsing worked.  we're done here
                    self.tracer.trace0("Parsed a local filesystem configuration")
                    return

                # try loading a remote file
                try:
                    self.tracer.trace0("Attempting a remote configuration file")
                    vrfCMDs = []
                    if self.vrf:
                        vrfCMDs = ["ip", "netns", "exec", f"ns-{self.vrf}"]

                    outputStr = subprocess.run(vrfCMDs + ["wget", "-qO", "-", value], text=True, stdout=subprocess.PIPE).stdout
                    configFile = io.StringIO(outputStr)
                    self.configs = self.parseConfig(configFile)
                except Exception as e:
                    print(e)
                    pass
                else:
                    # the remote file parse worked.  we're done here
                    self.tracer.trace0("Parsed a remote file configuration")
                    return

                self.tracer.trace0("Could not parse any configuration information!")

    def parseConfig(self, fileHandle):
        result = {}

        try:
            result = yaml.safe_load(fileHandle)
        except:
            # we failed loading yaml.  let's try json
            try:
                result = json.load(fileHandle)
            except:
                pass

        if not isinstance(result, dict) or len(result) == 0:
            self.tracer.trace0("Error loading the configuration")
            raise Exception("Error loading the configuration")

        # now we need to reformat all the macs, ouis, and lldpcaps to something consistent and usable
        for config in result['configs']:
            for ar in ['macs', 'ouis']:
                config['config'][ar] = list(map(formatMac, config['config'].get(ar, [])))

            config['config']['lldpCaps'] = self.convertListOfCapsToInt(config['config'].get('lldpCaps', None))
            self.tracer.trace1("config: {} lldpCap: {}".format(config['config']['name'], config['config']['lldpCaps']))

        self.tracer.trace0("- successfully loaded the config")

        return result

    def on_initialized(self):
        """ Callback provided by AgentHandler when all state is synchronized """
        # by default eossdk doesn't parse the options on load.  we need
        #  to fake the call this will return the option interfaces which
        #  we'll use to determine what to watch.  "all" or "" needs to
        #  reset the value to "" so that pulling the interfaces from pyeapi
        #  will just pull them all
        intfs = self.agentMgr_.agent_option("interfaces")
        self.on_agent_option("interfaces", intfs)

        configStr = self.agentMgr_.agent_option("config")
        self.on_agent_option("config", configStr)

        self.tracer.trace0("Fully initialized, running")
        self.tracer.trace5("full config: {}".format(self.configs))

    def on_oper_status(self, intfId, operState):
        """ Callback provided by IntfHandler when an interface's
        configuration changes """

        # when we get an interface state change we need to turn on mac address table monitoring.
        #  unfortunately with the sdk there is no way to filter the alerts based on interface
        #  so we will get a lot of mac address notices, potentially including for interfaces
        #  we have already processed and don't want to process again.
        intfStr = intfId.to_string()

        self.tracer.trace0("on_oper_status for {}".format(intfStr))

        if operState == eossdk.INTF_OPER_UP:
            # if we have a default linkup event type, let's set the port and let the rest of the
            #   logic take over from there
            portConfig = self.configs.get('default', [])
            if 'states' in portConfig and 'linkup' in portConfig['states']:
                defaultCommands = portConfig['states']['linkup']
                sessionID = uuid.uuid1()
                commandSequence = ['configure session {}'.format(sessionID),
                        'default interface {}'.format(intfStr),
                        'interface {}'.format(intfStr) ] +defaultCommands + ['commit']
                self.tracer.trace0("Defaulting interface {}".format(intfStr))
                self.pyeapi.config(commandSequence, autoComplete=True)
                
            # searching the list should probably be a really quick loop as there aren't likely
            #   to be a lot of interfaces in the coming up state at the same time
            self.enableInterface(intfStr, mac=True, lldp=True)

        elif operState == eossdk.INTF_OPER_DOWN:
            # set the default and remove this interface from the list.  if it's the last one,
            #  turn off mac table monitoring.  remove will except... is this better than a try
            #  block?
            self.disableInterface(intfStr, mac=True, lldp=True)

            # set the interface to a default if one exists
            portConfig = self.configs.get('default', [])
            if 'states' in portConfig and 'linkdown' in portConfig['states']:
                defaultCommands = portConfig['states']['linkdown']
                sessionID = uuid.uuid1()
                commandSequence = ['configure session {}'.format(sessionID),
                        'default interface {}'.format(intfStr),
                        'interface {}'.format(intfStr) ] +defaultCommands + ['commit']
                self.tracer.trace0("Defaulting interface {}".format(intfStr))
                self.pyeapi.config(commandSequence, autoComplete=True)

    # this function will handle enabling interface monitoring and setting
    #  up the sdk as needed
    def enableInterface(self, intfStr, mac=False, lldp=False):
        self.tracer.trace5("enableInterface")
        if mac:
            if intfStr not in self.macInterfaces:
                self.tracer.trace2("enabling {} for mac learning".format(intfStr))
                self.macInterfaces.append(intfStr)

            # this call should be a noop if it's already on, but start monitoring for mac
            #   learns in the mac table
            self.watch_all_mac_entries(True)
        if lldp:
            if intfStr not in self.lldpInterfaces:
                self.tracer.trace2("enabling {} for lldp learning".format(intfStr))
                self.lldpInterfaces.append(intfStr)

    # this function will handle disabling interface monitoring and resetting
    #  anything in the sdk to clean up as needed
    def disableInterface(self, intfStr, mac=False, lldp=False):
        if mac:
            if intfStr in self.macInterfaces:
                self.macInterfaces.remove(intfStr)

                if len(self.macInterfaces) == 0:
                    self.watch_all_mac_entries(False)
        if lldp:
            if intfStr in self.lldpInterfaces:
                self.lldpInterfaces.remove(intfStr)

    def on_mac_entry_set(self, mac):
        # .intfs() will return a set of all the interfaces that this mac has been found on
        #   we need to loop over all of them and set each interface accordingly
        intfIds = mac.intfs()
        for intf in intfIds:
            # loop over all the interfaces for this mac address.  if it is in our monitored
            #   list we can remove it and run the requisite change to the interface if there
            #   is a match
            intfStr = intf.to_string()
            if intfStr in self.macInterfaces:
                # we're processing this interface, regardless as to if there is a match.  we
                #   should remove it from the monitored list
                self.disableInterface(intfStr, mac=True, lldp=False)

                macStr = mac.mac_key().eth_addr().to_string()
                portConfig = self.searchMAC(formatMac(macStr))
                if not portConfig:
                    self.tracer.trace2("we didn't find a match for mac {}".format(macStr))
                    return

                if 'states' in portConfig and 'linkup' in portConfig['states']:
                    self.tracer.trace0("Setting a configuration on {}".format(intfStr))
                    self.configureInterface(intfStr, portConfig['states']['linkup'])

                    # if we've configured the interface based on mac we should not monitor
                    #  for lldp messages any longer
                    self.disableInterface(intfStr, mac=False, lldp=True)

    def on_lldp_intf_change(self, lldpNeighbor):
        # here we'll look at the handler for the lldp neighbor learning
        remoteSystem = self.lldpMgr.system_name(lldpNeighbor)
        caps = self.lldpMgr.system_capabilities(lldpNeighbor)
        intfStr = lldpNeighbor.intf().to_string()

        self.tracer.trace1("found a new lldp neighbor ***{}*** on ***{}***".format(remoteSystem, intfStr))
        if intfStr in self.lldpInterfaces:
            self.disableInterface(intfStr, mac=True, lldp=True)

            portConfig = self.searchLLDPCaps(caps)
            self.tracer.trace1(" -- config is {}".format(portConfig))

            if 'states' in portConfig and 'linkup' in portConfig['states']:
                self.tracer.trace0("Setting a configuration on {}".format(intfStr))
                self.configureInterface(intfStr, portConfig['states']['linkup'])

    # by default we will remove all configuration from the interface before adding new
    #  configuration specified in the conf file.  using a config session allows us to
    #  potentially apply an identical configuration on the interface without causing
    #  impact to network traffic
    def configureInterface(self, intfStr, portConfig):
        sessionID = uuid.uuid1()
        commandSequence = ['configure session {}'.format(sessionID),
                'default interface {}'.format(intfStr),
                'interface {}'.format(intfStr) ] + portConfig + ['commit']
        self.pyeapi.config(commandSequence, autoComplete=True)

    # this function will convert the lldp system capabilities to an integer
    #  bitmask, which is how it's stored.  unfortunately the sdk doesn't
    #  give me a way to get at that integer so i need to handle it myself
    #  the way this is written is likely fragile, but I don't see the python
    #  SDK defining the enum so we need to trust the documentation and that
    #  it'll never change
    def convertLLDPCapsToInt(self, lldpCaps):
        result = lldpCapsEnum.isOther
        
        if lldpCaps.repeater():
            result |= lldpCapsEnum.isRepeater
        if lldpCaps.bridge():
            result |= lldpCapsEnum.isBridge
        if lldpCaps.vlan_ap():
            result |= lldpCapsEnum.isAP
        if lldpCaps.router():
            result |= lldpCapsEnum.isRouter
        if lldpCaps.telephone():
            result |= lldpCapsEnum.isTelephone
        if lldpCaps.docsis():
            result |= lldpCapsEnum.isDocsis
        if lldpCaps.station():
            result |= lldpCapsEnum.isStation

        return result

    # this function will convert the configuration file list of string lldp capabilites
    #  to the integer form of them.  this function has the same caveats relating to the
    #  SDK as the above function convertLLDPCapsToInt
    def convertListOfCapsToInt(self, capsList):
        if capsList == None:
            return None

        result = lldpCapsEnum.isOther

        for cap in capsList:
            if cap == "isRepeater":
                result |= lldpCapsEnum.isRepeater
            elif cap == "isBridge":
                result |= lldpCapsEnum.isBridge
            elif cap == "isAP":
                result |= lldpCapsEnum.isAP
            elif cap == "isRouter":
                result |= lldpCapsEnum.isRouter
            elif cap == "isTelephone":
                result |= lldpCapsEnum.isTelephone
            elif cap == "isDocsis":
                result |= lldpCapsEnum.isDocsis
            elif cap == "isStation":
                result |= lldpCapsEnum.isStation

        return result

    def searchLLDPCaps(self, lldpCaps):
        lldpConfig = None
        lldpCapsInt = self.convertLLDPCapsToInt(lldpCaps)
        self.tracer.trace5("  searching for {}".format(lldpCapsInt))

        # main search loop
        self.tracer.trace1("searching for a match of lldp capabilities")
        for config in self.configs['configs']:
            self.tracer.trace5("   comparing to {}".format(config['config']['lldpCaps']))
            if config['config']['lldpCaps'] == lldpCapsInt:
                lldpConfig = config['config']
                break

        return lldpConfig

    # the searchMAC() function will loop over all configurations in the conf file
    #  and search for both an exact match, an oui match, then finally the default
    #  returning the configurations in that order, or None if there is no default
    def searchMAC(self, mac):
        ouiResult = None
        macResult = None

        # main search loop
        self.tracer.trace1("searching for {}".format(mac))
        for config in self.configs['configs']:
            # look for specific matches for each mac address in the mac table
            if mac in config['config']['macs']:
                self.tracer.trace1("found a specific match for {} in {}".format(mac, config))
                macResult = config['config']
            # look for oui matches
            if mac[:6] in config['config']['ouis']:
                self.tracer.trace1("found an oui match for {} in {}".format(mac, config))
                ouiResult = config['config']

        if macResult:
            return macResult
        elif ouiResult:
            return ouiResult
        else:
            # we didn't find any mac or oui match.  if there is a default, let's use it
            return self.configs.get('default', None)

if __name__ == "__main__":
    sdk = eossdk.Sdk()
    _ = InterfaceMonitor(sdk.get_intf_mgr(), sdk.get_agent_mgr(), sdk.get_mac_table_mgr(), sdk.get_lldp_mgr())
    sdk.main_loop(sys.argv)
