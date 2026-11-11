"""The HA status comes back while an interrupted recovery is on record (#1341).

get_ha_status used to read the recovery journal inside ha_lock. The journal read takes
that same non-reentrant lock again, through _ha_seen_since, so the status never came
back and the HA monitor waited behind it with the lock held. The status now reads under
the lock only what needs it, and asks for the interrupted recoveries once it is released.

  * with a lease in force and a guest a recovery moved, the status comes back in time
    and reports that recovery
  * with a lease in force and nothing on record, the status comes back with no list
  * the node states and health read under the lock are the same as before the fix
  * ha_lock is free again once the status is back

The manager is the one of test_ha_guard_rounds.pve(): no cluster, no session. The lease
question and the journal are replaced, as in the reproducer; the status itself is real.
"""
import copy
import threading
from unittest.mock import MagicMock

from pegaprox.core import ha as _ha
from test_ha_guard_rounds import pve

RUN = {'run': 'r1', 'cluster_id': 'c1', 'node': 'pve2', 'epoch': 1, 'instance_id': 'i1',
       'at': '2026-11-10T08:00:00+00:00', 'moved_at': '2026-11-10T08:00:05+00:00',
       'open': [], 'guests_open': [], 'moved': [101], 'held': []}


def _monitor(monkeypatch, runs, lease=True):
    """A manager with pve1 online and pve2 offline, no session, and the journal `runs`."""
    m = pve()
    m.is_connected = False
    m.ha_enabled, m.ha_check_interval, m.ha_failure_threshold = True, 10, 3
    m.ha_have_quorum, m.ha_last_quorum_check, m.ha_last_heartbeat_write = True, None, None
    m.ha_recovery_in_progress = {'pve2': True}
    m.ha_node_status = {
        'pve1': {'status': 'online', 'last_seen': None, 'consecutive_failures': 0},
        'pve2': {'status': 'offline', 'last_seen': None, 'consecutive_failures': 3},
    }
    m._create_session = MagicMock(side_effect=ConnectionError('no cluster in tests'))
    monkeypatch.setattr(_ha, 'lease_in_force', lambda: lease)
    monkeypatch.setattr(_ha, 'is_active', lambda: False)
    monkeypatch.setattr(_ha, 'recovery_leftovers', lambda *a, **kw: copy.deepcopy(runs))
    return m


def _guests(m, *rows):
    m.get_vm_resources = lambda *a, **k: [dict(zip(('vmid', 'node', 'status'), r), type='qemu')
                                          for r in rows]


def _call(m, seconds=2):
    """get_ha_status in a daemon thread: (still running after `seconds`, what it gave)."""
    out = {}

    def run():
        try:
            out['status'] = m.get_ha_status()
        except Exception as e:      # the test reports it; the thread must not raise
            out['error'] = repr(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return t.is_alive(), out


def test_the_ha_status_comes_back_and_reports_the_recovery_pve2_left_half_done(monkeypatch):
    """The HA page lists the guest 101 that pve2's recovery moved, and the monitor is not stuck."""
    m = _monitor(monkeypatch, [RUN])
    _guests(m, (101, 'pve1', 'stopped'))
    alive, out = _call(m)
    assert not alive, 'get_ha_status still waits for ha_lock after 2 s'
    assert 'error' not in out, out
    rec, = out['status']['interrupted_recoveries']
    assert (rec['run'], rec['node'], rec['moved'], rec['held']) == ('r1', 'pve2', [101], [])
    assert not m.ha_lock.locked()


def test_without_a_recovery_on_record_the_ha_status_comes_back_with_no_list(monkeypatch):
    """With a lease in force and nothing left by a former leader, the HA page shows no recovery."""
    m = _monitor(monkeypatch, [])
    alive, out = _call(m)
    assert not alive and 'error' not in out, out
    assert out['status']['interrupted_recoveries'] == []


def test_the_node_states_and_health_are_the_same_as_before(monkeypatch):
    """The HA page shows the node states the monitor last recorded, and the health from them."""
    m = _monitor(monkeypatch, [], lease=False)
    alive, out = _call(m)
    assert not alive and 'error' not in out, out
    st = out['status']
    assert st['nodes'] == {
        'pve1': {'status': 'online', 'last_seen': None, 'consecutive_failures': 0},
        'pve2': {'status': 'offline', 'last_seen': None, 'consecutive_failures': 3},
    }
    assert st['cluster_health'] == {'online_nodes': 1, 'total_nodes': 2,
                                    'is_2_node_cluster': True, 'status': 'degraded'}
    assert st['recovery_in_progress'] == ['pve2']
    assert 'interrupted_recoveries' not in st


def test_ha_lock_is_free_again_once_the_status_is_back(monkeypatch):
    """The HA monitor can take its lock again as soon as the HA page has been answered."""
    m = _monitor(monkeypatch, [])
    alive, out = _call(m)
    assert not alive and 'error' not in out, out
    assert m.ha_lock.acquire(blocking=False)
    m.ha_lock.release()
