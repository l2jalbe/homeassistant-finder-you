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

1. **ESP32 preferito** scelto nel menu della tapparella (vedi sotto), se online.
2. Miglior **RSSI diretto recente** (< 5 min, `TOPOLOGY_STALE_TIMEOUT`).
3. Miglior **RSSI diretto storico** (tapparelle fisse: il valore resta valido).
4. ESP32 con la migliore visibilità della mesh (la mesh rilancerà il comando).
5. Primo ESP32 disponibile.

`select_alternate_node` usa gli stessi criteri 2-5 escludendo il primario.

## ESP32 preferito (menu su ogni tapparella) — `preferred_nodes.py`

Ogni dispositivo tapparella ha il menu **ESP32 preferito** (sezione
*Configurazione*):

- `auto` = comportamento normale (punti 2-5 qui sopra);
- un ESP32, mostrato con il nome che ha in Home Assistant = quell'ESP32 viene
  provato per primo, poi gli altri, poi il cloud.

Dettagli:

- viene salvato l'**id del nodo ESPHome** (`finder_ble_2`), non il nome: se
  l'ESP32 viene rinominato in HA la scelta resta valida e il menu mostra il
  nuovo nome;
- l'elenco contiene tutti gli ESP32 visti almeno una volta; uno spento compare
  come `<nome> (non disponibile)`: la scelta resta salvata e nel frattempo la
  tapparella si comporta come `auto` (avviso nel log una volta sola);
- le scelte sono salvate in `.storage/finder_you_preferred_nodes` e non vengono
  toccate dagli aggiornamenti HACS; hanno effetto subito.

**Aggiornamento dalla v0.3.1 o precedenti:** il file
`/config/finder_you_mappa_personalizzata.yaml` non esiste più. Al primo avvio della v0.3.2 i valori diversi da `auto` vengono
importati nei menu e il file viene rinominato
`finder_you_mappa_personalizzata.yaml.importato` (si può cancellare).

## Riconoscimento degli ESP32

Ogni frame BLE inoltrato a HA porta il dispositivo ESPHome che l'ha ricevuto.
L'id del nodo si ricava, in quest'ordine, dal nome ESPHome registrato
(`device_name` della configurazione ESPHome, es. `finder-ble-2`) oppure dal
nome del dispositivo identico all'id. Non si usano corrispondenze parziali (fino
alla v0.3.1 bastava una parola in comune, per cui `finder-ble-2` veniva
scambiato per `finder_ble_1`). Il segnale di ogni tapparella è misurato
separatamente per ogni ESP32, anche quando due ESP32 ricevono lo stesso frame.

## Recupero dei comandi di gruppo (pulsanti e scenari) — `group_supervisor.py`

I pulsanti fisici e gli scenari YESLY comandano più tapparelle con **un solo
frame cifrato da 15 byte**:

```
45 0B 00 00  09  59 1E A1 C7  E2 15 00 00 8A 17
|  gruppo    cmd  firma        mittente
+- bit 0x40 = rilanciato, 6 bit bassi = progressivo della pressione
```

Non si può decifrare, ma l'effetto è visibile nei frame di stato (byte del
motore). Il supervisore:

1. **Impara** (per ogni gruppo = byte 1-3) quali tapparelle *avviano* il motore
   nei 3,5 s successivi e in che direzione, e associa ogni codice comando
   (byte 4) ad "apri"/"chiudi". Un gruppo è noto dopo 2 osservazioni con
   movimento; una tapparella è membro se si è mossa almeno 2 volte e in almeno
   metà delle osservazioni; un codice è affidabile dopo 2 osservazioni coerenti
   (≥ 80%). Dati salvati in `.storage/finder_you_groups`.
2. **Recupera**: a un nuovo comando di un gruppo noto con codice noto, dopo
   2,5 s ogni membro che non ha avviato il motore *nella direzione giusta* riceve
   il comando singolo (`async_set_position`: BLE locale, poi cloud).

Regole di sicurezza: mai per codici sconosciuti; mai se nessun membro si è mosso
nella direzione attesa; mai se la tapparella è già al bersaglio (±2%); una sola
correzione per comando; annullato se arriva un nuovo comando dello stesso gruppo;
le tapparelle comandate da HA negli stessi secondi sono ignorate (né apprese né
corrette). Solo gli *avvii* del motore contano: una tapparella che continua a
muoversi nella vecchia direzione dopo aver perso il comando viene corretta.

Le impostazioni si gestiscono da Home Assistant: ogni gruppo noto è un
dispositivo **"Gruppo Finder `<codice>`"** (`group_entities.py`) con
l'interruttore **Correzione automatica**, un interruttore **Includi
`<tapparella>`** per ogni membro, i sensori **Correzioni** e **Osservazioni**
(membri e codici negli attributi), il pulsante **Dimentica gruppo** e i menu
**Aggiungi tapparella** / **Rimuovi tapparella aggiunta a mano**.

**Membri manuali** (v0.3.1): una tapparella aggiunta a mano è sempre membro,
anche se l'apprendimento non l'ha mai vista muoversi (tipicamente segnale
≤ −90 dBm: l'ESP32 non sente l'avvio del motore). Come prova che il comando di
gruppo è stato eseguito contano solo i membri **appresi**. Se l'ESP32 non sente
un membro manuale, HA lo "corregge" anche quando si stava già muovendo: è
innocuo (stesso bersaglio) ma fa salire il contatore Correzioni.
Rinominando il dispositivo si dà un nome al gruppo (usato anche nei log). I
dispositivi compaiono da soli quando un gruppo diventa noto o acquisisce un
membro (segnale dispatcher `finder_you_groups_updated`). Impostazioni e dati
appresi stanno in `.storage/finder_you_groups`. Il sensore **Group
corrections** del gateway conta i recuperi totali.

## Regole dell'autore originale (rimosse)

Il fork di francescodoffizi conteneva regole scritte per la sua casa:
`NODE_ADJACENCY_MAP` (stanze adiacenti tra i suoi 6 ESP32),
`SOURCE_MAC_TO_NODE_MAP` (MAC fisici dei suoi ESP32) e due mappe di parole
chiave stanza → ESP32 (`_get_affinity_node` e l'alias map in
`_resolve_esphome_node`). Non si attivavano con altri nomi di ESP32 e sono state
rimosse nella v0.1.4. Il codice originale è nella cronologia git al commit
`bb921a6` (`git show bb921a6:custom_components/finder_you/topology.py`).

## Diagnostica del segnale (`signal_tracker.py`)

Ogni frame di stato **diretto** aggiunge un campione RSSI (per ESP32); ogni
frame **rilanciato** aggiorna l'ultima ricezione via mesh. Il sensore
**Segnale BLE** pubblica la media dei campioni al massimo una volta al minuto
(storico leggero); **Ricezione** vale *diretta* se la tapparella è stata sentita
direttamente negli ultimi 7 giorni, *debole* se diretta ma con media sotto
−90 dBm, altrimenti *solo_mesh*.

## Nodo ESP32 (`esphome/`)

`finder-ble-1.yaml` + `finder_relay.h`: il nodo inoltra ogni advertising Finder
come evento `esphome.finder_ble_adv` scartando solo i frame identici dallo
stesso mittente entro 2,5 s (il componente originale limitava a 1 evento/s per
MAC e perdeva i frame dei gateway). Un ripetitore radio dei comandi da 15 byte
è stato provato (firmware v2) e rimosso: non riduceva i comandi persi.

## Frame BLE Finder utili da ricordare (13S2)

```
diretto  05:61:06:00:F3:EB   06 00 F3 EB  B1  3D  00  00 42 01 13
rilancio 05:61:03:20:F2:3E   46 00 F3 EB  B1  3D  00  00 42 01 13
                             └─ origine ─┘    pos mov     seq
```

- byte 0-3: ultime 4 parti del MAC di origine; nel rilancio il byte 0 ha il bit `0x40`
- byte 5: posizione 0-100 (100 = aperta)
- byte 6: motore `0x01` in salita, `0x10` in discesa, `0x00` fermo
- frame di 9 byte = comandi singoli (contatore + comando + MIC AES-CCM), non stato
- frame di 15 byte = comandi di gruppo da pulsanti/scenari (vedi sopra)
