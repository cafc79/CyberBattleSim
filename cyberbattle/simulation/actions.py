# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
    actions.py
    This file contains the class and associated methods for the AgentActions
    class which interacts directly with the environment. It is the class
    which both the user and RL agents should manipulate the environment.
"""

from dataclasses import dataclass
import dataclasses
from datetime import time
import boolean
from collections import OrderedDict
import logging
from enum import Enum
from typing import Iterator, List, NamedTuple, Optional, Set, Tuple, Dict, TypedDict, cast
import IPython.core.display as d
import pandas as pd

from cyberbattle.simulation.model import MachineStatus, PrivilegeLevel, PropertyName, VulnerabilityID, VulnerabilityType
from . import model


logger = logging.getLogger(__name__)
Reward = float

DiscoveredNodeInfo = TypedDict('DiscoveredNodeInfo', {
    'id': model.NodeID,
    'status': str
})


class Penalty:
    """Penalties (=negative reward) returned for some actions taken in the simulation"""
    # penalty for generic suspiciousness
    SUPSPICIOUSNESS = -5.0

    # penalty for attempting a connection to a port that was not open
    SCANNING_UNOPEN_PORT = -10.0

    # penalty for repeating the same exploit attempt
    REPEAT = -1

    LOCAL_EXPLOIT_FAILED = -20
    FAILED_REMOTE_EXPLOIT = -50

    # penalty for attempting to connect or execute an action on a node that's not in running state
    MACHINE_NOT_RUNNING = 0

    # penalty for attempting a connection with an invalid password
    WRONG_PASSWORD = -10

    # traffice blocked by outoing rule in a local firewall
    BLOCKED_BY_LOCAL_FIREWALL = -10

    # traffice blocked by incoming rule in a remote firewall
    BLOCKED_BY_REMOTE_FIREWALL = -10


# Reward for any successfully executed local or remote attack
# (the attack cost gets substracted from this reward)
SUCCEEDED_ATTACK_REWARD = 50


class EdgeAnnotation(Enum):
    """Annotation added to the network edges created as the simulation is played"""
    KNOWS = 0
    REMOTE_EXPLOIT = 1
    LATERAL_MOVE = 2


class ActionResult(NamedTuple):
    """Result from executing an action"""
    reward: Reward
    outcome: Optional[model.VulnerabilityOutcome]


ALGEBRA = boolean.BooleanAlgebra()
ALGEBRA.TRUE.dual = type(ALGEBRA.FALSE)
ALGEBRA.FALSE.dual = type(ALGEBRA.TRUE)


@dataclass
class NodeTrackingInformation:
    """Track information about nodes gathered throughout the simulation"""
    # Map (vulnid, local_or_remote) to time of last attack.
    # local_or_remote is true for local, false for remote
    last_attack: Dict[Tuple[model.VulnerabilityID, bool], time] = dataclasses.field(default_factory=dict)
    # Last time another node connected to this node
    last_connection: Optional[time] = None
    # All node properties discovered so far
    discovered_properties: Set[int] = dataclasses.field(default_factory=set)


class AgentActions:
    """
        This is the AgentActions class. It interacts with and makes changes to the environment.
    """

    def __init__(self, environment: model.Environment):
        """
            AgentActions Constructor
        """
        self._environment = environment
        self._gathered_credentials: Set[model.CredentialID] = set()
        self._discovered_nodes: "OrderedDict[model.NodeID, NodeTrackingInformation]" = OrderedDict()

        # List of all special tags indicating a privilege level reached on a node
        self.privilege_tags = [model.PrivilegeEscalation(p).tag for p in list(PrivilegeLevel)]

        # Mark all owned nodes as discovered
        for i, node in environment.nodes():
            if node.agent_installed:
                self.__mark_node_as_owned(i, PrivilegeLevel.LocalUser)

    def discovered_nodes(self) -> Iterator[Tuple[model.NodeID, model.NodeInfo]]:
        for node_id in self._discovered_nodes:
            yield (node_id, self._environment.get_node(node_id))

    def _check_prerequisites(self, target: model.NodeID, vulnerability: model.VulnerabilityInfo) -> bool:
        """
        This is a quick helper function to check the prerequisites to see if
        they match the ones supplied.
        """
        node: model.NodeInfo = self._environment.network.nodes[target]['data']
        node_flags = node.properties
        expr = vulnerability.precondition.expression

        # this line seems redundant but it is necessary to declare the symbols used in the mapping
        # pylint: disable=unused-variable

        mapping = {i: ALGEBRA.TRUE if str(i) in node_flags else ALGEBRA.FALSE
                   for i in expr.get_symbols()}
        is_true: bool = cast(boolean.Expression, expr.subs(mapping)).simplify() == ALGEBRA.TRUE
        return is_true

    def list_vulnerabilities_in_target(
            self,
            target: model.NodeID,
            type_filter: Optional[model.VulnerabilityType] = None) -> List[model.VulnerabilityID]:
        """
            This function takes a model.NodeID for the target to be scanned
            and returns a list of vulnerability IDs.
            It checks each vulnerability in the library against the the properties of a given node
            and determines which vulnerabilities it has.
        """
        if not self._environment.network.has_node(target):
            raise ValueError(f"invalid node id '{target}'")

        target_node_data: model.NodeInfo = self._environment.get_node(target)

        global_vuln: Set[model.VulnerabilityID] = {
            vuln_id
            for vuln_id, vulnerability in self._environment.vulnerability_library.items()
            if (type_filter is None or vulnerability.type == type_filter)
            and self._check_prerequisites(target, vulnerability)
        }

        local_vuln: Set[model.VulnerabilityID] = {
            vuln_id
            for vuln_id, vulnerability in target_node_data.vulnerabilities.items()
            if (type_filter is None or vulnerability.type == type_filter)
            and self._check_prerequisites(target, vulnerability)
        }

        return list(global_vuln.union(local_vuln))

    def __annotate_edge(self, source_node_id: model.NodeID,
                        target_node_id: model.NodeID,
                        new_annotation: EdgeAnnotation) -> None:
        """Create the edge if it does not already exist, and annotate with the maximum
        of the existing annotation and a specified new annotation"""
        edge_annotation = self._environment.network.get_edge_data(source_node_id, target_node_id)
        if edge_annotation is not None:
            if 'kind' in edge_annotation:
                new_annotation = EdgeAnnotation(max(edge_annotation['kind'].value, new_annotation.value))
            else:
                new_annotation = new_annotation.value
        self._environment.network.add_edge(source_node_id, target_node_id, kind=new_annotation, kind_as_float=float(new_annotation.value))

    def get_discovered_properties(self, node_id: model.NodeID) -> Set[int]:
        return self._discovered_nodes[node_id].discovered_properties

    def __mark_node_as_discovered(self, node_id: model.NodeID) -> None:
        logger.info('discovered node: ' + node_id)
        if node_id not in self._discovered_nodes:
            self._discovered_nodes[node_id] = NodeTrackingInformation()

    def __mark_nodeproperties_as_discovered(self, node_id: model.NodeID, properties: List[PropertyName]):
        properties_indices = [self._environment.identifiers.properties.index(p)
                              for p in properties
                              if p not in self.privilege_tags]

        if node_id in self._discovered_nodes:
            self._discovered_nodes[node_id].discovered_properties = self._discovered_nodes[node_id].discovered_properties.union(properties_indices)
        else:
            self._discovered_nodes[node_id] = NodeTrackingInformation(discovered_properties=set(properties_indices))

    def __mark_allnodeproperties_as_discovered(self, node_id: model.NodeID):
        node_info: model.NodeInfo = self._environment.network.nodes[node_id]['data']
        self.__mark_nodeproperties_as_discovered(node_id, node_info.properties)

    def __mark_node_as_owned(self,
                             node_id: model.NodeID,
                             privilege: PrivilegeLevel = model.PrivilegeLevel.LocalUser) -> None:
        if node_id not in self._discovered_nodes:
            self._discovered_nodes[node_id] = NodeTrackingInformation()
        node_info = self._environment.get_node(node_id)
        node_info.agent_installed = True
        node_info.privilege_level = model.escalate(node_info.privilege_level, privilege)
        self._environment.network.nodes[node_id].update({'data': node_info})

        self.__mark_allnodeproperties_as_discovered(node_id)

    def __mark_discovered_entities(self, reference_node: model.NodeID, outcome: model.VulnerabilityOutcome) -> None:
        if isinstance(outcome, model.LeakedCredentials):
            for credential in outcome.credentials:
                self.__mark_node_as_discovered(credential.node)
                self._gathered_credentials.add(credential.credential)
                logger.info('discovered credential: ' + str(credential))
                self.__annotate_edge(reference_node, credential.node, EdgeAnnotation.KNOWS)

        elif isinstance(outcome, model.LeakedNodesId):
            for node_id in outcome.nodes:
                self.__mark_node_as_discovered(node_id)
                self.__annotate_edge(reference_node, node_id, EdgeAnnotation.KNOWS)

    def get_node_privilegelevel(self, node_id: model.NodeID) -> model.PrivilegeLevel:
        """Return the last recorded privilege level of the specified node"""
        node_info = self._environment.get_node(node_id)
        return node_info.privilege_level

    def get_nodes_with_atleast_privilegelevel(self, level: PrivilegeLevel) -> List[model.NodeID]:
        """Return all nodes with at least the specified privilege level"""
        return [n for n, info in self._environment.nodes() if info.privilege_level >= level]

    def is_node_discovered(self, node_id: model.NodeID) -> bool:
        """Returns true if previous actions have revealed the specified node ID"""
        return node_id in self._discovered_nodes

    def __process_outcome(self,
                          expected_type: VulnerabilityType,
                          vulnerability_id: VulnerabilityID,
                          node_id: model.NodeID,
                          node_info: model.NodeInfo,
                          local_or_remote: bool,
                          failed_penalty: float,
                          throw_if_vulnerability_not_present: bool
                          ) -> Tuple[bool, ActionResult]:

        if node_info.status != model.MachineStatus.Running:
            logger.info("target machine not in running state")
            return False, ActionResult(reward=Penalty.MACHINE_NOT_RUNNING,
                                       outcome=None)

        is_global_vulnerability = vulnerability_id in self._environment.vulnerability_library
        is_inplace_vulnerability = vulnerability_id in node_info.vulnerabilities

        if is_global_vulnerability:
            vulnerabilities = self._environment.vulnerability_library
        elif is_inplace_vulnerability:
            vulnerabilities = node_info.vulnerabilities
        else:
            if throw_if_vulnerability_not_present:
                raise ValueError(f"Vulnerability '{vulnerability_id}' not supported by node='{node_id}'")
            else:
                logger.info(f"Vulnerability '{vulnerability_id}' not supported by node '{node_id}'")
                return False, ActionResult(reward=Penalty.SUPSPICIOUSNESS, outcome=None)

        vulnerability = vulnerabilities[vulnerability_id]

        outcome = vulnerability.outcome

        if vulnerability.type != expected_type:
            raise ValueError(f"vulnerability id '{vulnerability_id}' is for an attack of type {vulnerability.type}, expecting: {expected_type}")

        # check vulnerability prerequisites
        if not self._check_prerequisites(node_id, vulnerability):
            return False, ActionResult(reward=failed_penalty, outcome=model.ExploitFailed())

        # if the vulnerability type is a privilege escalation
        # and if the escalation level is not already reached on that node,
        # then add the escalation tag to the node properties
        if isinstance(outcome, model.PrivilegeEscalation):
            if outcome.tag in node_info.properties:
                return False, ActionResult(reward=Penalty.REPEAT, outcome=outcome)

            self.__mark_node_as_owned(node_id, outcome.level)

            node_info.properties.append(outcome.tag)

        elif isinstance(outcome, model.ProbeSucceeded):
            for p in outcome.discovered_properties:
                assert p in node_info.properties, \
                    f'Discovered property {p} must belong to the set of properties associated with the node.'

            self.__mark_nodeproperties_as_discovered(node_id, outcome.discovered_properties)

        if node_id not in self._discovered_nodes:
            self._discovered_nodes[node_id] = NodeTrackingInformation()

        lookup_key = (vulnerability_id, local_or_remote)

        already_executed = lookup_key in self._discovered_nodes[node_id].last_attack

        if already_executed:
            last_time = self._discovered_nodes[node_id].last_attack[lookup_key]
            if node_info.last_reimaging is None or last_time >= node_info.last_reimaging:
                return False, ActionResult(reward=Penalty.REPEAT, outcome=outcome)

        self._discovered_nodes[node_id].last_attack[lookup_key] = time()

        self.__mark_discovered_entities(node_id, outcome)
        logger.info("GOT REWARD: " + vulnerability.reward_string)
        return True, ActionResult(reward=0.0 if already_executed else SUCCEEDED_ATTACK_REWARD - vulnerability.cost,
                                  outcome=vulnerability.outcome)

    def exploit_remote_vulnerability(self,
                                     node_id: model.NodeID,
                                     target_node_id: model.NodeID,
                                     vulnerability_id: model.VulnerabilityID
                                     ) -> ActionResult:
        """
        Attempt to exploit a remote vulnerability
        from a source node to another node using the specified
        vulnerability.
        """
        if node_id not in self._environment.network.nodes:
            raise ValueError(f"invalid node id '{node_id}'")
        if target_node_id not in self._environment.network.nodes:
            raise ValueError(f"invalid target node id '{target_node_id}'")

        source_node_info: model.NodeInfo = self._environment.get_node(node_id)
        target_node_info: model.NodeInfo = self._environment.get_node(target_node_id)

        if not source_node_info.agent_installed:
            raise ValueError("Agent does not owned the source node '" + node_id + "'")

        if target_node_id not in self._discovered_nodes:
            raise ValueError("Agent has not discovered the target node '" + target_node_id + "'")

        succeeded, result = self.__process_outcome(
            model.VulnerabilityType.REMOTE,
            vulnerability_id,
            target_node_id,
            target_node_info,
            local_or_remote=False,
            failed_penalty=Penalty.FAILED_REMOTE_EXPLOIT,
            # We do not throw if the vulnerability is missing in order to
            # allow agent attempts to explore potential remote vulnerabilities
            throw_if_vulnerability_not_present=False
        )

        if succeeded:
            self.__annotate_edge(node_id, target_node_id, EdgeAnnotation.REMOTE_EXPLOIT)

        return result

    def exploit_local_vulnerability(self, node_id: model.NodeID,
                                    vulnerability_id: model.VulnerabilityID) -> ActionResult:
        """
            This function exploits a local vulnerability on a node
            it takes a nodeID for the target and a vulnerability ID.

            It returns either a vulnerabilityoutcome object or None
        """
        graph = self._environment.network
        if node_id not in graph.nodes:
            raise ValueError(f"invalid node id '{node_id}'")

        node_info = self._environment.get_node(node_id)

        if not node_info.agent_installed:
            raise ValueError(f"Agent does not owned the node '{node_id}'")

        succeeded, result = self.__process_outcome(
            model.VulnerabilityType.LOCAL,
            vulnerability_id,
            node_id, node_info,
            local_or_remote=True,
            failed_penalty=Penalty.LOCAL_EXPLOIT_FAILED,
            throw_if_vulnerability_not_present=True)

        return result

    def __is_passing_firewall_rules(self, rules: List[model.FirewallRule], port_name: model.PortName) -> bool:
        """Determine if traffic on the specified port is permitted by the specified sets of firewall rules"""
        for rule in rules:
            if rule.port == port_name:
                if rule.permission == model.RulePermission.ALLOW:
                    return True
                else:
                    logger.debug(f'BLOCKED TRAFFIC - PORT \'{port_name}\' Reason: ' + rule.reason)
                    return False

        logger.debug(f"BLOCKED TRAFFIC - PORT '{port_name}' - Reason: no rule defined for this port.")
        return False

    def connect_to_remote_machine(
            self,
            source_node_id: model.NodeID,
            target_node_id: model.NodeID,
            port_name: model.PortName,
            credential: model.CredentialID) -> ActionResult:
        """
            This function connects to a remote machine with credential as opposed to via an exploit.
            It takes a NodeId for the source machine, a NodeID for the target Machine, and a credential object
            for the credential.
        """
        graph = self._environment.network
        if source_node_id not in graph.nodes:
            raise ValueError(f"invalid node id '{source_node_id}'")
        if target_node_id not in graph.nodes:
            raise ValueError(f"invalid node id '{target_node_id}''")

        target_node = self._environment.get_node(target_node_id)
        source_node = self._environment.get_node(source_node_id)
        # ensures that the source node is owned by the agent
        # and that the target node is discovered

        if not source_node.agent_installed:
            raise ValueError(f"Agent does not owned the source node '{source_node_id}'")

        if target_node_id not in self._discovered_nodes:
            raise ValueError(f"Agent has not discovered the target node '{target_node_id}'")

        if credential not in self._gathered_credentials:
            raise ValueError(f"Agent has not discovered credential '{credential}'")

        if not self.__is_passing_firewall_rules(source_node.firewall.outgoing, port_name):
            logger.info(f"BLOCKED TRAFFIC: source node '{source_node_id}'" +
                        f" is blocking outgoing traffic on port '{port_name}'")
            return ActionResult(reward=Penalty.BLOCKED_BY_LOCAL_FIREWALL,
                                outcome=None)

        if not self.__is_passing_firewall_rules(target_node.firewall.incoming, port_name):
            logger.info(f"BLOCKED TRAFFIC: target node '{target_node_id}'" +
                        f" is blocking outgoing traffic on port '{port_name}'")
            return ActionResult(reward=Penalty.BLOCKED_BY_REMOTE_FIREWALL,
                                outcome=None)

        target_node_is_listening = port_name in [i.name for i in target_node.services]
        if not target_node_is_listening:
            logger.info(f"target node '{target_node_id}' not listening on port '{port_name}'")
            return ActionResult(reward=Penalty.SCANNING_UNOPEN_PORT,
                                outcome=None)
        else:
            target_node_data: model.NodeInfo = self._environment.get_node(target_node_id)

            if target_node_data.status != model.MachineStatus.Running:
                logger.info("target machine not in running state")
                return ActionResult(reward=Penalty.MACHINE_NOT_RUNNING,
                                    outcome=None)

            # check the credentials before connecting
            if not self._check_service_running_and_authorized(target_node_data, port_name, credential):
                logger.info("invalid credentials supplied")
                return ActionResult(reward=Penalty.WRONG_PASSWORD,
                                    outcome=None)

            is_already_owned = target_node_data.agent_installed
            if is_already_owned:
                return ActionResult(reward=Penalty.REPEAT,
                                    outcome=model.LateralMove())

            if target_node_id not in self._discovered_nodes:
                self._discovered_nodes[target_node_id] = NodeTrackingInformation()

            was_previously_owned_at = self._discovered_nodes[target_node_id].last_connection
            self._discovered_nodes[target_node_id].last_connection = time()

            if was_previously_owned_at is not None and \
                target_node_data.last_reimaging is not None and \
                    was_previously_owned_at >= target_node_data.last_reimaging:
                return ActionResult(reward=Penalty.REPEAT, outcome=model.LateralMove())

            self.__annotate_edge(source_node_id, target_node_id, EdgeAnnotation.LATERAL_MOVE)
            self.__mark_node_as_owned(target_node_id)
            logger.info(f"Infected node '{target_node_id}' from '{source_node_id}'" +
                        f" via {port_name} with credential '{credential}'")
            if target_node.owned_string:
                logger.info("Owned message: " + target_node.owned_string)

            return ActionResult(reward=float(target_node_data.value) if was_previously_owned_at is None else 0.0,
                                outcome=model.LateralMove())

    def _check_service_running_and_authorized(self,
                                              target_node_data: model.NodeInfo,
                                              port_name: model.PortName,
                                              credential: model.CredentialID) -> bool:
        """
            This is a quick helper function to check the prerequisites to see if
            they match the ones supplied.
        """
        for service in target_node_data.services:
            if service.running and service.name == port_name and credential in service.allowedCredentials:
                return True
        return False

    def list_nodes(self) -> List[DiscoveredNodeInfo]:
        """Returns the list of nodes ID that were discovered or owned by the attacker."""
        return [cast(DiscoveredNodeInfo, {'id': node_id,
                                          'status': 'owned' if node_info.agent_installed else 'discovered'
                                          })
                for node_id, node_info in self.discovered_nodes()
                ]

    def list_remote_attacks(self, node_id: model.NodeID) -> List[model.VulnerabilityID]:
        """Return list of all remote attacks that may be executed onto the specified node."""
        attacks: List[model.VulnerabilityID] = self.list_vulnerabilities_in_target(
            node_id, model.VulnerabilityType.REMOTE)
        return attacks

    def list_local_attacks(self, node_id: model.NodeID) -> List[model.VulnerabilityID]:
        """Return list of all local attacks that may be executed onto the specified node."""
        attacks: List[model.VulnerabilityID] = self.list_vulnerabilities_in_target(
            node_id, model.VulnerabilityType.LOCAL)
        return attacks

    def list_attacks(self, node_id: model.NodeID) -> List[model.VulnerabilityID]:
        """Return list of all attacks that may be executed on the specified node."""
        attacks: List[model.VulnerabilityID] = self.list_vulnerabilities_in_target(
            node_id)
        return attacks

    def list_all_attacks(self) -> List[Dict[str, object]]:
        """List all possible attacks from all the nodes currently owned by the attacker"""
        on_owned_nodes: List[Dict[str, object]] = [
            {'id': n['id'],
             'status': n['status'],
             'properties': self._environment.get_node(n['id']).properties,
             'local_attacks': self.list_local_attacks(n['id']),
             'remote_attacks': self.list_remote_attacks(n['id'])
             }
            for n in self.list_nodes() if n['status'] == 'owned']
        on_discovered_nodes: List[Dict[str, object]] = [{'id': n['id'],
                                                         'status': n['status'],
                                                         'local_attacks': None,
                                                         'remote_attacks': self.list_remote_attacks(n['id'])}
                                                        for n in self.list_nodes() if n['status'] != 'owned']
        return on_owned_nodes + on_discovered_nodes

    def print_all_attacks(self) -> None:
        """Pretty print list of all possible attacks from all the nodes currently owned by the attacker"""
        d.display(pd.DataFrame.from_dict(self.list_all_attacks()))  # type: ignore


class DefenderAgentActions:
    """Actions reserved to defender agents"""

    # Number of steps it takes to completely reimage a node
    REIMAGING_DURATION = 15

    def __init__(self, environment: model.Environment):
        # map nodes being reimaged to the remaining number of steps to completion
        self.node_reimaging_progress: Dict[model.NodeID, int] = dict()

        # Last calculated availability of the network
        self.__network_availability: float = 1.0

        self._environment = environment

    @property
    def network_availability(self):
        return self.__network_availability

    def reimage_node(self, node_id: model.NodeID):
        """Re-image a computer node"""
        # Mark the node for re-imaging and make it unavailable until re-imaging completes
        self.node_reimaging_progress[node_id] = self.REIMAGING_DURATION

        node_info = self._environment.get_node(node_id)
        assert node_info.reimagable, f'Node {node_id} is not re-imageable'

        node_info.agent_installed = False
        node_info.privilege_level = model.PrivilegeLevel.NoAccess
        node_info.status = model.MachineStatus.Imaging
        node_info.last_reimaging = time()
        self._environment.network.nodes[node_id].update({'data': node_info})

    def on_attacker_step_taken(self):
        """Function to be called each time a step is take in the simulation"""
        for node_id in list(self.node_reimaging_progress.keys()):
            remaining_steps = self.node_reimaging_progress[node_id]
            if remaining_steps > 0:
                self.node_reimaging_progress[node_id] -= 1
            else:
                logger.info(f"Machine re-imaging completed: {node_id}")
                node_data = self._environment.get_node(node_id)
                node_data.status = model.MachineStatus.Running
                self.node_reimaging_progress.pop(node_id)

        # Calculate the network availability metric based on machines
        # and services that are running
        total_node_weights = 0
        network_node_availability = 0
        for node_id, node_info in self._environment.nodes():
            total_service_weights = 0
            running_service_weights = 0
            for service in node_info.services:
                total_service_weights += service.sla_weight
                running_service_weights += service.sla_weight * int(service.running)

            if node_info.status == MachineStatus.Running:
                adjusted_node_availability = (1 + running_service_weights) / (1 + total_service_weights)
            else:
                adjusted_node_availability = 0.0

            total_node_weights += node_info.sla_weight
            network_node_availability += adjusted_node_availability * node_info.sla_weight

        self.__network_availability = network_node_availability / total_node_weights
        assert(self.__network_availability <= 1.0 and self.__network_availability >= 0.0)

    def override_firewall_rule(self, node_id: model.NodeID, port_name: model.PortName, incoming: bool, permission: model.RulePermission):
        node_data = self._environment.get_node(node_id)
        rules = node_data.firewall.incoming if incoming else node_data.firewall.outgoing
        matching_rules = [r for r in rules if r.port == port_name]
        if matching_rules:
            for r in matching_rules:
                r.permission = permission
        else:
            new_rule = model.FirewallRule(port_name, permission)
            if incoming:
                node_data.firewall.incoming = [new_rule] + node_data.firewall.incoming
            else:
                node_data.firewall.outgoing = [new_rule] + node_data.firewall.outgoing

    def block_traffic(self, node_id: model.NodeID, port_name: model.PortName, incoming: bool):
        return self.override_firewall_rule(node_id, port_name, incoming, permission=model.RulePermission.BLOCK)

    def allow_traffic(self, node_id: model.NodeID, port_name: model.PortName, incoming: bool):
        return self.override_firewall_rule(node_id, port_name, incoming, permission=model.RulePermission.ALLOW)

    def stop_service(self, node_id: model.NodeID, port_name: model.PortName):
        node_data = self._environment.get_node(node_id)
        assert node_data.status == model.MachineStatus.Running, "Machine must be running to stop a service"
        for service in node_data.services:
            if service.name == port_name:
                service.running = False

    def start_service(self, node_id: model.NodeID, port_name: model.PortName):
        node_data = self._environment.get_node(node_id)
        assert node_data.status == model.MachineStatus.Running, "Machine must be running to start a service"
        for service in node_data.services:
            if service.name == port_name:
                service.running = True
