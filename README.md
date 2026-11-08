<img src="custom_components/finder_you/brand/icon.png" alt="Finder YOU" width="96" align="right">

# Finder YOU (YESLY) per Home Assistant — fork con supporto gateway 1Y.GU.005 ed ESP32

Integrazione personalizzata per comandare le **tapparelle Finder YESLY (13S2)**
da Home Assistant, tramite il cloud Finder YOU e, se presenti, uno o più
**ESP32 con ESPHome** che parlano direttamente in Bluetooth con gli attuatori.

Questo repository è un fork di
[`francescodoffizi/homeassistant-finder-you`](https://github.com/francescodoffizi/homeassistant-finder-you),
a sua volta derivato da
[`7AC/homeassistant-finder-you`](https://github.com/7AC/homeassistant-finder-you).
Il fork nasce per far funzionare l'integrazione su un impianto con
**gateway 1Y.GU.005** e per rendere la parte ESP32 utilizzabile su qualsiasi
casa, non solo su quella dell'autore originale.

> Versione attuale: **0.2.0** — dettagli in [CHANGELOG.md](CHANGELOG.md).

---

## Cosa cambia rispetto al progetto originale

### 1. Gateway 1Y.GU.005: sessione cloud stabile

Su impianti con gateway **1Y.GU.005** il cloud Finder si comporta
diversamente da quanto documentato in [PROTOCOL.md](PROTOCOL.md):

- l'iscrizione all'impianto (`subscribe-plant`) risponde con 4 byte
  (`10 01 40 03`) invece dello snapshot di stato;
- `GetPlant` non contiene posizione né movimento delle tapparelle
  (campi `#12`/`#13` assenti);
- nessuna notifica push quando si usano i pulsanti a muro;
- il gateway **resta legato alla prima sessione cloud**: dopo una
  riconnessione i comandi vengono accettati dal cloud ma non eseguiti,
  finché il gateway non viene riavviato.

L'integrazione originale, non vedendo mai telemetria, si riconnetteva quasi a
ogni comando e smetteva di funzionare dopo il primo. Questo fork aggiunge
l'opzione **"Sessione cloud stabile"** (*Finder YOU → Configura*):

- nessuna riconnessione preventiva, nessun "chiudi-riapri-riprova";
- i timeout occasionali della lettura periodica non chiudono la sessione
  (riconnessione solo dopo 3 fallimenti consecutivi o su errore di rete reale);
- lettura periodica ogni 120 s.

Inoltre: **stop anche senza ESP32** (invia la posizione stimata in base ai
tempi di corsa) e il dispositivo diagnostico **"Gateway Finder YOU"** con il
contatore **Cloud reconnections**.

### 2. ESP32: stato in tempo reale e mesh YESLY

Con uno o più ESP32 (firmware basato su
[`francescodoffizi/BLE_Finder_Yesly`](https://github.com/francescodoffizi/BLE_Finder_Yesly))
i comandi partono in Bluetooth locale e gli stati arrivano dalle tapparelle
stesse, compresi i movimenti fatti dai **pulsanti a muro**.

Novità di questo fork:

- **Frame rilanciati dalla mesh decodificati.** I frame di stato ripetuti
  dagli altri dispositivi YESLY non sono cifrati e contengono il MAC della
  tapparella di origine: ora vengono usati invece di essere scartati. Un solo
  ESP32 riceve così lo stato anche delle tapparelle che non sente direttamente.
- **"In apertura" / "In chiusura"** mostrati in HA mentre il motore gira.
- **Conferma BLE in 2 s** (prima 3,5 s) prima di ritentare da un altro ESP32 o
  passare al cloud; nel log, per ogni comando, tempo di conferma e strada
  (diretta o via mesh).
- **Scelta dell'ESP32 solo in base al segnale**: rimosse le regole scritte per
  la casa dell'autore originale (nomi dei suoi ESP32, stanze, MAC).
- **Mappa personalizzata** `/config/finder_you_mappa_personalizzata.yaml`:
  generata da sola con tutte le tapparelle a `auto`; scrivendo il nome di un
  ESP32 al posto di `auto` quell'ESP32 viene provato per primo. Dettagli in
  [ROUTING.md](ROUTING.md).
- **Recupero dei comandi di gruppo**: quando un pulsante fisico o uno scenario
  comanda più tapparelle e una non risponde, HA se ne accorge e le manda il
  comando singolarmente. I gruppi vengono imparati da soli osservando quali
  tapparelle si muovono; si gestiscono nella sezione `gruppi:` della mappa
  personalizzata. Dettagli in [ROUTING.md](ROUTING.md).
- Icona dell'integrazione (Home Assistant 2026.3+).

---

## Installazione (HACS)

1. HACS → ⋮ → **Repository personalizzati**
2. URL: `https://github.com/l2jalbe/homeassistant-finder-you` — categoria **Integration**
3. Scarica **Finder YOU** e riavvia Home Assistant
4. *Impostazioni → Dispositivi e servizi → Aggiungi integrazione → Finder YOU*
5. Email e password dell'app Finder YOU (l'account deve essere
   **proprietario** dell'impianto; serve email + password, l'accesso con
   Google non è supportato)
6. Con gateway 1Y.GU.005: *Finder YOU → Configura* → attiva
   **Sessione cloud stabile**

### Se i comandi non rispondono (gateway 1Y.GU.005)

Togli e rimetti l'alimentazione al gateway, aspetta 3 minuti, poi
*Finder YOU → ⋮ → Ricarica*. Evita di tenere aperta l'app Finder YOU: il cloud
gestisce un solo client attivo alla volta.

## ESP32 (opzionale, consigliato)

- Scheda: ESP32 classico (ESP32-WROOM-32, `board: esp32dev`), ESPHome 2026.10+.
- Configurazione d'esempio: componente `finder_yesly` di
  [BLE_Finder_Yesly](https://github.com/francescodoffizi/BLE_Finder_Yesly)
  come `external_components`, servizi `finder_mesh_click`,
  `finder_mesh_set_percent`, `finder_mesh_shutter_stop` e inoltro degli
  advertising Finder come evento `esphome.finder_ble_adv`.
- Nel dispositivo ESPHome in HA attiva **"Allow the device to perform Home
  Assistant actions"**, altrimenti gli stati non arrivano.
- L'inoltro degli advertising deve essere senza limiti per MAC (i gateway
  rilanciano frame di molte tapparelle con lo stesso MAC): vedi
  `finder_relay.h` nella documentazione del nodo.
- Un ESP32 in posizione centrale può bastare grazie alla mesh; altri ESP32
  migliorano i comandi verso le tapparelle più isolate (vedi [ROUTING.md](ROUTING.md)).

## Limiti noti

- Con gateway 1Y.GU.005 il cloud non fornisce gli stati: senza ESP32 HA
  conosce solo la posizione richiesta dai propri comandi.
- Se la sessione cloud cade davvero (rete, riavvio di HA, app aperta) può
  servire riavviare il gateway.
- Un solo impianto per account.

## Documentazione

- [CHANGELOG.md](CHANGELOG.md) — modifiche versione per versione
- [ROUTING.md](ROUTING.md) — scelta dell'ESP32, mappa personalizzata, formato dei frame BLE
- [PROTOCOL.md](PROTOCOL.md) — protocollo cloud Finder (dal progetto originale)

## Crediti

- [7AC/homeassistant-finder-you](https://github.com/7AC/homeassistant-finder-you) — integrazione originale e reverse engineering del cloud
- [francescodoffizi](https://github.com/francescodoffizi) — fork con BLE locale, luci, scenari e firmware ESP32
- [`hpack`](https://pypi.org/project/hpack/), [`condatek/finderblissha`](https://github.com/condatek/finderblissha)

Licenza MIT. Progetto non ufficiale, non affiliato a Finder S.p.A.; nomi e
marchi Finder, YESLY e Finder YOU appartengono ai rispettivi proprietari.
