"""User-editable routing map: which ESP32 node to try FIRST for each shutter.

The file lives in the Home Assistant config folder
(``/config/finder_you_mappa_personalizzata.yaml``). It is generated
automatically with every shutter of the plant set to ``auto``; the user
replaces ``auto`` with an ESPHome node name to force that node as first
choice. ``auto`` restores the normal behaviour (best RSSI). See ROUTING.md.

Rules:
- the file is (re)written only when it is missing, when a shutter is not
  listed yet, or when the list of detected ESP32 nodes changes; existing
  values are always preserved, comments added by hand are not;
- changes are picked up at the next poll (stable session: within 2 min)
  or immediately on integration reload — no HA restart needed;
- an unknown node name, or a node that is currently offline, is ignored
  (logged once) and the shutter behaves as ``auto``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)

FILE_NAME = "finder_you_mappa_personalizzata.yaml"
AUTO = "auto"
SECTION = "tapparelle"

_HEADER = """\
# =============================================================================
# FINDER YOU - MAPPA PERSONALIZZATA (ESP32 prioritario per ogni tapparella)
# =============================================================================
# Questo file viene creato e aggiornato AUTOMATICAMENTE dall'integrazione
# Finder YOU con l'elenco di tutte le tapparelle dell'impianto.
#
# COME SI COMPILA
#   Per ogni tapparella il valore puo' essere:
#     auto           -> comportamento normale: HA sceglie l'ESP32 con il
#                       segnale migliore (consigliato)
#     <nome ESP32>   -> HA prova PRIMA questo ESP32; se la tapparella non
#                       conferma, prova gli altri ESP32 e infine il cloud
#
#   Esempio:
#     tapparelle:
#       "Cucina finestra sx": finder_ble_2
#       "Cucina finestra dx": finder_ble_2
#       "1P Letto3 finestra": finder_ble_3
#       "Lavanderia": auto
#
#   Il nome dell'ESP32 e' quello del dispositivo ESPHome con i trattini
#   oppure i trattini bassi (finder-ble-2 e finder_ble_2 vanno bene entrambi).
#   Per tornare al comportamento normale rimetti: auto
#
# QUANDO HANNO EFFETTO LE MODIFICHE
#   Alla lettura periodica successiva (entro circa 2 minuti) oppure subito
#   ricaricando l'integrazione (Finder YOU > tre puntini > Ricarica).
#   Non serve riavviare Home Assistant.
#
# GRUPPI DI PULSANTI E SCENARI
#   Si gestiscono da Home Assistant: ogni gruppo imparato e' un dispositivo
#   "Gruppo Finder ..." con gli interruttori per la correzione automatica e
#   per includere/escludere le tapparelle (non da questo file).
#
# NOTE
#   - Non cambiare i nomi delle tapparelle (sono quelli dell'app Finder YOU).
#   - Le nuove tapparelle vengono aggiunte da sole con valore auto.
#   - I valori che scrivi vengono sempre conservati; eventuali commenti
#     aggiunti a mano invece vengono persi quando il file viene riscritto.
#   - Un nome di ESP32 sbagliato o un ESP32 spento vengono ignorati (la
#     tapparella si comporta come auto) e segnalati nel log di HA.
#   - Documentazione: ROUTING.md nel repository dell'integrazione.
# =============================================================================
"""


def normalize_node(value: str) -> str:
    return value.strip().lower().replace("-", "_")


class CustomRoutingMap:
    """Load/generate the routing map and answer "preferred node" queries."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._by_name: dict[str, str] = {}  # shutter name -> raw value
        self._mtime: float | None = None
        self._nodes_written: str | None = None
        self._warned: set[tuple[str, str]] = set()
        self._uuid_by_name: dict[str, str] = {}
        self._legacy_groups = False  # file still has the v0.2.0 "gruppi" section

    @property
    def path(self) -> str:
        return self._path

    # ---- blocking helpers (run in executor) --------------------------------

    def _read_file(self) -> tuple[dict[str, str], str | None]:
        with open(self._path, encoding="utf-8") as fh:
            text = fh.read()
        nodes_line = None
        for line in text.splitlines():
            if line.startswith("# ESP32 rilevati:"):
                nodes_line = line.split(":", 1)[1].strip()
                break
        data = yaml.safe_load(text) or {}
        self._legacy_groups = isinstance(data, dict) and "gruppi" in data
        section = data.get(SECTION) if isinstance(data, dict) else None
        out: dict[str, str] = {}
        if isinstance(section, dict):
            for k, v in section.items():
                if k is None:
                    continue
                out[str(k).strip()] = AUTO if v is None else str(v).strip()
        return out, nodes_line

    def _write_file(self, names: list[tuple[str, str]], nodes: list[str]) -> None:
        nodes_txt = ", ".join(nodes) if nodes else "(nessuno al momento)"
        lines = [_HEADER, f"# ESP32 rilevati: {nodes_txt}", f"{SECTION}:"]
        for name, uuid in names:
            value = self._by_name.get(name, AUTO) or AUTO
            lines.append(f"  {json.dumps(name, ensure_ascii=False)}: {value}   # uuid {uuid[:8]}")
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, self._path)
        self._mtime = os.path.getmtime(self._path)
        self._nodes_written = nodes_txt

    def sync(self, shutters: list[tuple[str, str]], nodes: list[str]) -> None:
        """Reload the file if changed and (re)generate it when needed.

        ``shutters`` is a list of (name, uuid); ``nodes`` the detected
        ESPHome node names. Blocking: call from an executor.
        """
        names = []
        seen: set[str] = set()
        for name, uuid in shutters:
            n = (name or "").strip() or uuid[:8]
            if n in seen:  # duplicate names: disambiguate with the uuid
                n = f"{n} ({uuid[:8]})"
            seen.add(n)
            names.append((n, uuid))
        self._uuid_by_name = {n: u for n, u in names}
        nodes = sorted(nodes)
        nodes_txt = ", ".join(nodes) if nodes else "(nessuno al momento)"

        exists = os.path.exists(self._path)
        file_nodes = None
        if exists:
            mtime = os.path.getmtime(self._path)
            if mtime != self._mtime or self._nodes_written is None:
                try:
                    self._by_name, file_nodes = self._read_file()
                    self._mtime = mtime
                    self._nodes_written = file_nodes
                    self._warned.clear()
                    _LOGGER.info("Loaded routing map %s (%d entries)", self._path, len(self._by_name))
                except Exception as err:  # noqa: BLE001
                    # Keep the last good values; never overwrite a file the
                    # user is editing and that currently has a syntax error.
                    _LOGGER.warning("Cannot read %s (%s); keeping previous values", self._path, err)
                    return

        missing = [n for n, _ in names if n not in self._by_name]
        if not exists or missing or self._nodes_written != nodes_txt or self._legacy_groups:
            self._legacy_groups = False
            self._write_file(names, nodes)
            _LOGGER.info(
                "Routing map %s %s (%d shutters, nodes: %s)",
                self._path,
                "created" if not exists else "updated",
                len(names),
                nodes_txt,
            )

    # ---- queries (event loop) -------------------------------------------

    def preferred_node(self, uuid: str, available_nodes: set[str]) -> str | None:
        """Return the node to try first for ``uuid``, or None for auto."""
        name = next((n for n, u in self._uuid_by_name.items() if u == uuid), None)
        if name is None:
            return None
        raw = self._by_name.get(name, AUTO)
        if not raw or raw.lower() == AUTO:
            return None
        wanted = normalize_node(raw)
        for node in available_nodes:
            if normalize_node(node) == wanted:
                return node
        key = (name, raw)
        if key not in self._warned:
            self._warned.add(key)
            _LOGGER.warning(
                "Routing map: ESP32 '%s' for '%s' not found or offline (available: %s); using auto",
                raw,
                name,
                ", ".join(sorted(available_nodes)) or "none",
            )
        return None

    def as_dict(self) -> dict[str, Any]:
        return dict(self._by_name)
