"""Group command supervisor: learn button/scenario groups and recover misses.

YESLY wall buttons and scenarios drive several shutters with ONE encrypted
15-byte mesh frame, e.g. (relayed copy)::

    45 0B 00 00  09  59 1E A1 C7  E2 15 00 00 8A 17
    |  group     cmd  MIC          sender
    +- 0x40 relayed flag | press sequence (low 6 bits)

We cannot decrypt it, but we can observe what happens next through the
plaintext status frames (motion byte). This module:

1. LEARNS, per group id (bytes 1-3), which shutters start moving in the
   3.5 s after the command and in which direction, and maps each command
   code (byte 4) to "open"/"close" from the observed direction.
2. RECOVERS: on a later command of a known group with a known code, any
   learned member that has not started moving after CORRECTION_DELAY
   seconds - while at least one other member did move the expected way -
   and is not already at the target gets an individual set_position
   (local BLE first, cloud fallback) from the coordinator.

Safety rules: never for unknown codes, never if no member moved, never if
the shutter is already at the target, one correction per command, aborted
when a newer command of the same group arrives, shutters commanded by HA
around the same time are ignored. Per-group settings (name, on/off,
excluded shutters) live in the user routing map file (routing_map.py).
See ROUTING.md.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    from .coordinator import FinderYouCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "finder_you_groups"
STORAGE_VERSION = 1

COMMAND_FRAME_LEN = 15
COMMAND_DEDUP_S = 10.0
MOTION_MIN_DELAY_S = 0.2       # motion earlier than this is not caused by the command
CORRECTION_DELAY_S = 2.5       # wait this long for members to start moving
LEARN_WINDOW_S = 3.5           # motion within this window counts for learning
HA_COMMAND_GUARD_S = 6.0       # shutters commanded by HA this close are ignored

MIN_GROUP_OBSERVATIONS = 2     # observations with movement before a group is "known"
MIN_MEMBER_COUNT = 2           # times a shutter must have moved with the group
MIN_MEMBER_RATIO = 0.5         # ... and in at least this share of observations
MIN_CODE_COUNT = 2             # consistent observations before a code is trusted
MIN_CODE_RATIO = 0.8

DIRECTION_TARGET = {"open": 100, "close": 0}


def parse_command_frame(byte_list: list[int]) -> tuple[str, int, int] | None:
    """Return (group_id, code, seq) for a 15-byte YESLY command frame."""
    if len(byte_list) != COMMAND_FRAME_LEN:
        return None
    group_id = "".join(f"{b:02X}" for b in byte_list[1:4])
    return group_id, byte_list[4], byte_list[0] & 0x3F


@dataclass
class _Observation:
    group_id: str
    code: int
    seq: int
    ts: float
    superseded: bool = False
    corrected: set[str] = field(default_factory=set)


class GroupSupervisor:
    """Learns group membership and corrects shutters that missed a command."""

    def __init__(self, hass: HomeAssistant, coordinator: FinderYouCoordinator) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # group_id -> {"obs": int, "members": {uuid: n}, "codes": {"09": {"open": n, "close": n}}}
        self._groups: dict[str, dict[str, Any]] = {}
        self._seen: dict[tuple[str, int, int], float] = {}
        self._active: dict[str, _Observation] = {}
        self._motion_start: dict[str, tuple[float, str]] = {}
        self._motion_now: dict[str, str | None] = {}
        self._ha_cmd_ts: dict[str, float] = {}
        self._unsubs: list = []
        self.corrections_total = 0

    # ---- persistence -------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._groups = data.get("groups", {}) or {}
            self.corrections_total = int(data.get("corrections_total", 0))
            _LOGGER.info("Loaded %d learned YESLY groups", len(self._groups))

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"groups": self._groups, "corrections_total": self.corrections_total}, 10
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ---- inputs ----------------------------------------------------------

    @callback
    def note_ha_command(self, uuid: str) -> None:
        """A shutter was commanded by HA (user, automation or correction)."""
        self._ha_cmd_ts[uuid] = time.time()

    @callback
    def note_motion(self, uuid: str, motion: str | None) -> None:
        """Motor state from a status frame ("opening"/"closing"/None=stopped).

        Only motion STARTS (or reversals) are recorded: a shutter that keeps
        moving the old way after missing a command must not look like it
        reacted to it.
        """
        direction = {"opening": "open", "closing": "close"}.get(motion or "")
        if direction is None:
            self._motion_now[uuid] = None
            return
        if self._motion_now.get(uuid) != direction:
            self._motion_now[uuid] = direction
            self._motion_start[uuid] = (time.time(), direction)

    @callback
    def handle_command_frame(self, byte_list: list[int]) -> None:
        parsed = parse_command_frame(byte_list)
        if parsed is None:
            return
        group_id, code, seq = parsed
        now = time.time()
        key = (group_id, code, seq)
        if now - self._seen.get(key, 0) < COMMAND_DEDUP_S:
            return
        self._seen[key] = now
        if len(self._seen) > 200:
            self._seen = {k: t for k, t in self._seen.items() if now - t < 60}

        prev = self._active.get(group_id)
        if prev is not None:
            prev.superseded = True
        obs = _Observation(group_id, code, seq, now)
        self._active[group_id] = obs
        _LOGGER.debug("Group command %s code %02X seq %d", group_id, code, seq)

        # HassJob runs plain lambdas in the executor: mark them as callbacks
        self._unsubs = self._unsubs[-20:]
        self._unsubs.append(
            async_call_later(self.hass, CORRECTION_DELAY_S, callback(lambda _t: self._async_correct(obs)))
        )
        self._unsubs.append(
            async_call_later(self.hass, LEARN_WINDOW_S, callback(lambda _t: self._async_learn(obs)))
        )

    # ---- learned model ---------------------------------------------------

    def _movers(self, obs: _Observation, until: float) -> dict[str, str]:
        out: dict[str, str] = {}
        for uuid, (ts, direction) in self._motion_start.items():
            if obs.ts + MOTION_MIN_DELAY_S <= ts <= until:
                if abs(self._ha_cmd_ts.get(uuid, 0) - obs.ts) < HA_COMMAND_GUARD_S and uuid not in obs.corrected:
                    continue  # moved because HA commanded it
                out[uuid] = direction
        return out

    def members(self, group_id: str) -> list[str]:
        g = self._groups.get(group_id)
        if not g:
            return []
        obs = max(1, int(g.get("obs", 0)))
        return [
            u
            for u, n in g.get("members", {}).items()
            if n >= MIN_MEMBER_COUNT and n / obs >= MIN_MEMBER_RATIO
        ]

    def code_direction(self, group_id: str, code: int) -> str | None:
        g = self._groups.get(group_id)
        if not g:
            return None
        counts = g.get("codes", {}).get(f"{code:02X}", {})
        total = sum(counts.values())
        if not total:
            return None
        direction, n = max(counts.items(), key=lambda kv: kv[1])
        if n >= MIN_CODE_COUNT and n / total >= MIN_CODE_RATIO:
            return direction
        return None

    def is_known(self, group_id: str) -> bool:
        g = self._groups.get(group_id)
        return bool(g) and int(g.get("obs", 0)) >= MIN_GROUP_OBSERVATIONS and bool(self.members(group_id))

    def summary(self) -> dict[str, dict[str, Any]]:
        """Readable view for the routing map file."""
        names = {s.uuid: s.name.strip() for s in self._coordinator.shutters}
        out: dict[str, dict[str, Any]] = {}
        for gid, g in sorted(self._groups.items()):
            codes = {}
            for code_hex in sorted(g.get("codes", {})):
                d = self.code_direction(gid, int(code_hex, 16))
                codes[code_hex] = {"open": "apri", "close": "chiudi"}.get(d or "", "?")
            out[gid] = {
                "membri": sorted(names.get(u, u[:8]) for u in self.members(gid)),
                "codici": codes,
                "osservazioni": int(g.get("obs", 0)),
            }
        return out

    # ---- timers ------------------------------------------------------------

    @callback
    def _async_learn(self, obs: _Observation) -> None:
        movers = self._movers(obs, obs.ts + LEARN_WINDOW_S)
        for uuid in obs.corrected:
            movers.setdefault(uuid, "")  # corrected members stay members
        real = {u: d for u, d in movers.items() if d}
        if not real:
            return
        g = self._groups.setdefault(obs.group_id, {"obs": 0, "members": {}, "codes": {}})
        g["obs"] = int(g.get("obs", 0)) + 1
        for uuid in movers:
            g["members"][uuid] = int(g["members"].get(uuid, 0)) + 1
        dirs = list(real.values())
        majority = max(set(dirs), key=dirs.count)
        code_counts = g["codes"].setdefault(f"{obs.code:02X}", {})
        code_counts[majority] = int(code_counts.get(majority, 0)) + 1
        _LOGGER.info(
            "Learned group %s code %02X -> %s (%d shutter(s) moved, observation #%d)",
            obs.group_id,
            obs.code,
            majority,
            len(real),
            g["obs"],
        )
        self._save()

    @callback
    def _async_correct(self, obs: _Observation) -> None:
        if obs.superseded or not self.is_known(obs.group_id):
            return
        settings = self._coordinator.routing_map.group_settings(obs.group_id)
        if not settings.get("enabled", True):
            return
        direction = self.code_direction(obs.group_id, obs.code)
        if direction not in DIRECTION_TARGET:
            return
        target = DIRECTION_TARGET[direction]
        movers = self._movers(obs, obs.ts + CORRECTION_DELAY_S + 0.5)
        if not any(d == direction for d in movers.values()):
            return  # no evidence the group command was executed: do nothing
        excluded = settings.get("excluded", set())
        names = {s.uuid: s.name.strip() for s in self._coordinator.shutters}
        for uuid in self.members(obs.group_id):
            if movers.get(uuid) == direction or names.get(uuid, "") in excluded:
                continue  # reacted the right way (a start the other way does not count)
            if time.time() - self._ha_cmd_ts.get(uuid, 0) < HA_COMMAND_GUARD_S:
                continue
            pos = (self._coordinator.data or {}).get(uuid)
            if isinstance(pos, (int, float)) and abs(pos - target) <= 2:
                continue  # already where the group wants it
            obs.corrected.add(uuid)
            self.corrections_total += 1
            label = settings.get("name") or obs.group_id
            _LOGGER.warning(
                "Group '%s' %s: %s did not react within %.1fs - sending individual command %d%%",
                label,
                "apri" if direction == "open" else "chiudi",
                names.get(uuid, uuid),
                CORRECTION_DELAY_S,
                target,
            )
            self.note_ha_command(uuid)
            self.hass.async_create_task(self._coordinator.async_set_position(uuid, target))
        if obs.corrected:
            self._save()
