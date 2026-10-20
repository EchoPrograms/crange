"""Prepare and audit a core-weighted Corosync voting configuration.

This module never changes live cluster configuration, expected votes, or node
power state. An operator validates and installs its reviewed output separately.
"""
from itertools import combinations
import re


class RedundancyError(ValueError):
    pass


def quorum_failure_matrix(votes, core_node, tolerated_failures=3):
    if (core_node not in votes or not 1 <= len(votes) <= 12 or
            type(tolerated_failures) is not int or not 0 <= tolerated_failures < len(votes) or
            any(type(value) is not int or value < 1 for value in votes.values())):
        raise RedundancyError('Invalid node voting/failure model.')
    expected = sum(votes.values())
    quorum = expected // 2 + 1
    peers = sorted(set(votes) - {core_node})
    cases = []
    for count in range(tolerated_failures + 1):
        for failed in combinations(peers, count):
            remaining = expected - sum(votes[node] for node in failed)
            cases.append({'failed_nodes': list(failed), 'remaining_votes': remaining,
                          'quorate': remaining >= quorum})
    return {'core_node': core_node, 'votes': votes, 'expected_votes': expected,
            'quorum': quorum, 'tolerated_failures': tolerated_failures,
            'cases': cases, 'all_cases_quorate': all(case['quorate'] for case in cases)}


def prepare_quorum_config(text, core_node='proxmox4', tolerated_failures=3):
    """Only change the core's vote and advance the configuration version.

    Fail closed on alternate quorum mechanisms or nonstandard existing votes.
    Addresses, membership, node IDs and cluster transport remain byte-identical.
    """
    clean = re.sub(r'#.*', '', text)
    if re.search(r'^\s*(expected_votes|votes|two_node|last_man_standing|auto_tie_breaker|allow_downscale|expected_votes_tracking)\s*:', clean, re.M):
        raise RedundancyError('An alternate quorum policy is present; manual review is required.')
    if re.search(r'\bdevice\s*\{', clean):
        raise RedundancyError('A quorum device is configured; manual review is required.')
    providers = re.findall(r'^\s*provider\s*:\s*(\S+)\s*$', clean, re.M)
    if providers != ['corosync_votequorum']:
        raise RedundancyError('Exactly one votequorum provider is required.')
    blocks = list(re.finditer(r'(?ms)^\s*node\s*\{[^{}]*?^\s*\}', text))
    names, votes, core_block = [], {}, None
    for block in blocks:
        body = re.sub(r'#.*', '', block.group())
        name = re.findall(r'^\s*name\s*:\s*([A-Za-z0-9_-]+)\s*$', body, re.M)
        vote = re.findall(r'^\s*quorum_votes\s*:\s*(\d+)\s*$', body, re.M)
        if len(name) != 1 or len(vote) != 1 or name[0] in votes:
            raise RedundancyError('Each node needs a unique name and explicit vote count.')
        names.append(name[0]); votes[name[0]] = int(vote[0])
        if name[0] == core_node:
            core_block = block
    if not core_block or not 2 <= len(names) <= 12:
        raise RedundancyError('Core node or valid cluster membership is missing.')
    if type(tolerated_failures) is not int or not 0 <= tolerated_failures < len(names):
        raise RedundancyError('Failure tolerance must be below the node count.')
    if any(value != 1 for name, value in votes.items() if name != core_node):
        raise RedundancyError('Existing non-core votes require manual review.')
    # w + (N-1-F) must exceed half of w + (N-1).
    weight = max(1, 2 * tolerated_failures - len(names) + 2)
    if votes[core_node] not in (1, weight):
        raise RedundancyError('Existing core vote count requires manual review.')
    proposed = {**votes, core_node: weight}
    audit = quorum_failure_matrix(proposed, core_node, tolerated_failures)
    if not audit['all_cases_quorate']:
        raise RedundancyError('Proposed votes do not satisfy the failure model.')
    versions = list(re.finditer(r'(?m)^(\s*config_version\s*:\s*)(\d+)(\s*)$', text))
    if len(versions) != 1:
        raise RedundancyError('Exactly one configuration version is required.')
    version = int(versions[0].group(2))
    changed = votes != proposed
    result = text
    if changed:
        replacement = re.sub(r'(?m)^(\s*quorum_votes\s*:\s*)\d+(\s*)$',
                             lambda m: m.group(1) + str(weight) + m.group(2), core_block.group())
        result = text[:core_block.start()] + replacement + text[core_block.end():]
        result = re.sub(r'(?m)^(\s*config_version\s*:\s*)\d+(\s*)$',
                        lambda m: m.group(1) + str(version + 1) + m.group(2), result)
    return result, {**audit, 'changed': changed, 'previous_votes': votes,
                    'previous_config_version': version,
                    'config_version': version + int(changed)}
