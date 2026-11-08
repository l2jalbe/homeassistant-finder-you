# Come l'integrazione sceglie l'ESP32 (routing BLE)

Nota di progetto per il fork `l2jalbe/homeassistant-finder-you`. Spiega come
vengono instradati i comandi verso le tapparelle e dove mettere le mani se
servono regole speciali.

## Percorso di un comando (tapparella)

`coordinator.py` → `async_set_position()`:

1. Se il BLE locale è attivo e la tapparella è nell'inventario BLE (MAC + chiave
   AES ricavati dal cloud), sceglie l'ESP32 con `select_best_node()` e
   un'alternativa con `select_alternate_node()` (`topology.py`).
2. Chiama il servizio `esphome.<nodo>_finder_mesh_set_percent` sull'ESP32.
3. Aspetta la **conferma**: un frame di stato della tapparella (diretto o
   rilanciato dalla mesh) per `BLE_SHUTTER_ACK_TIMEOUT` = **2,0 s**.
   - Se il segnale primario è debole (< −85 dBm) trasmette anche dal nodo
     alternativo dopo 0,55 s.
   - Senza conferma ritenta dal nodo alternativo (altri 2,0 s).
4. Ancora nessuna conferma → stesso comando **via cloud** (idempotente:
   "vai al X%" ripetuto non sposta la tapparella).

Nel log (livello INFO) ogni conferma riporta il tempo e la strada:

```
Local BLE shutter command 61% acknowledged by Cucina finestra sx in 840 ms
(sent via 'finder_ble_1', confirmation mesh relay 05:61:03:20:F2:3E via 'finder_ble_1')
```

Questi tempi servono per decidere se ridurre ancora `BLE_SHUTTER_ACK_TIMEOUT`.

## Ordine di scelta dell'ESP32 (`select_best_node`)

1. **Mappa personalizzata** dell'utente (vedi sotto), se l'ESP32 indicato è online.
2. Miglior **RSSI diretto recente** (< 5 min, `TOPOLOGY_STALE_TIMEOUT`).
3. Miglior **RSSI diretto storico** (tapparelle fisse: il valore resta valido).
4. ESP32 con la migliore visibilità della mesh (la mesh rilancerà il comando).
5. Primo ESP32 disponibile.

`select_alternate_node` usa gli stessi criteri 2-5 escludendo il primario.

## Mappa personalizzata: `/config/finder_you_mappa_personalizzata.yaml`

Gestita da `routing_map.py`.

- Viene **creata da sola** al primo avvio con tutte le tapparelle a `auto` e con
  un commento iniziale che spiega come compilarla.
- Si aggiorna da sola quando compaiono nuove tapparelle o cambia l'elenco
  degli ESP32 rilevati (riga `# ESP32 rilevati: ...`). I valori scritti
  dall'utente vengono sempre conservati, i commenti aggiunti a mano no.
- Le modifiche vengono rilette alla lettura periodica (entro ~2 min con la
  sessione stabile) o subito ricaricando l'integrazione.
- Valore `auto` = comportamento normale; nome ESP32 (`finder_ble_2` o
  `finder-ble-2`) = quell'ESP32 viene provato per primo, poi gli altri, poi il cloud.
- ESP32 sconosciuto/spento → ignorato con un avviso nel log, la tapparella
  si comporta come `auto`.
- File con errore di sintassi → non viene mai sovrascritto; restano validi gli
  ultimi valori letti correttamente.

Esempio:

```yaml
tapparelle:
  "Cucina finestra sx": finder_ble_2
  "Cucina finestra dx": finder_ble_2
  "1P Letto3 finestra": finder_ble_3
  "Lavanderia": auto
```

### Estendere la mappa

- **Luci / relè**: `select_best_node()` riceve già il `BleDirectDevice`, quindi
  basta generare nel file una sezione `luci:` (come `tapparelle:` in
  `routing_map.py`) e passare anche `coordinator.lights` / `switches` a
  `routing_map.sync()`.
- **Ordine completo** (es. "prima esp1, poi esp3"): trasformare il valore in una
  lista e far restituire a `preferred_node()` il primo online; in
  `select_alternate_node()` usare il secondo.

## Regole dell'autore originale (rimosse)

Il fork di francescodoffizi conteneva regole scritte per la sua casa:
`NODE_ADJACENCY_MAP` (stanze adiacenti tra i suoi 6 ESP32),
`SOURCE_MAC_TO_NODE_MAP` (MAC fisici dei suoi ESP32) e due mappe di parole
chiave stanza → ESP32 (`_get_affinity_node` e l'alias map in
`_resolve_esphome_node`). Non si attivavano con altri nomi di ESP32 e sono state
rimosse nella v0.1.4. Il codice originale è nella cronologia git al commit
`bb921a6` (`git show bb921a6:custom_components/finder_you/topology.py`).

## Frame BLE Finder utili da ricordare (13S2)

```
diretto  05:61:06:00:F3:EB   06 00 F3 EB  B1  3D  00  00 42 01 13
rilancio 05:61:03:20:F2:3E   46 00 F3 EB  B1  3D  00  00 42 01 13
                             └─ origine ─┘    pos mov     seq
```

- byte 0-3: ultime 4 parti del MAC di origine; nel rilancio il byte 0 ha il bit `0x40`
- byte 5: posizione 0-100 (100 = aperta)
- byte 6: motore `0x01` in salita, `0x10` in discesa, `0x00` fermo
- frame di 9 byte = comandi (contatore + comando + MIC AES-CCM), non stato
