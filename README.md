<img src="custom_components/finder_you/brand/icon.png" alt="Finder YOU" width="96" align="right">

# Finder YOU (YESLY) per Home Assistant — fork con gateway 1Y.GU.005, ESP32 e recupero comandi

Integrazione personalizzata per comandare le **tapparelle Finder YESLY (13S2)**
da Home Assistant tramite il cloud Finder YOU e, soprattutto, tramite uno o più
**ESP32 con ESPHome** che parlano direttamente in Bluetooth con gli attuatori.

Fork di
[`francescodoffizi/homeassistant-finder-you`](https://github.com/francescodoffizi/homeassistant-finder-you),
a sua volta derivato da
[`7AC/homeassistant-finder-you`](https://github.com/7AC/homeassistant-finder-you).
Nasce per far funzionare l'integrazione su un impianto con **gateway
1Y.GU.005**, renderla utilizzabile su qualsiasi casa e aggiungere funzioni che
il sistema Finder non offre.

> Versione attuale: **0.3.0** — dettagli in [CHANGELOG.md](CHANGELOG.md).

---

## In breve

| Funzione | Come |
|---|---|
| Comandi alle tapparelle | ESP32 in Bluetooth locale, cloud come riserva |
| Stato in tempo reale (anche dai pulsanti a muro) | frame di stato BLE, diretti o rilanciati dalla mesh |
| "In apertura" / "In chiusura" | byte del motore nei frame di stato |
| Stop | BLE immediato; senza ESP32 posizione stimata via cloud |
| Pulsanti e scenari che "perdono" una tapparella | **recupero automatico** dei gruppi appresi |
| Qualità del segnale per tapparella | sensori **Segnale BLE** e **Ricezione** con storico |
| Scenari dell'app Finder YOU | entità `scene.*` raccolte in un unico dispositivo |
| Gateway 1Y.GU.005 | opzione **Sessione cloud stabile** |

---

## Cosa trovi in Home Assistant

**Per ogni tapparella** (dispositivo con il nome dell'app Finder YOU)
- `cover.*` — apri, chiudi, stop, posizione, stato "in apertura/in chiusura"
- **Segnale BLE** (dBm, diagnostica) — media al minuto del segnale con cui
  l'ESP32 riceve direttamente la tapparella; nel grafico vedi l'andamento,
  negli attributi l'ESP32 migliore e il valore per ogni ESP32
- **Ricezione** (diagnostica) — *Diretta* oppure *Solo via mesh*: il modo più
  rapido per individuare le tapparelle critiche

**Scenari Finder YOU** — un solo dispositivo con tutte le `scene.*` dell'app
(eseguite dal gateway via cloud).

**Gateway Finder YOU** — diagnostica della sessione cloud: *Cloud
reconnections*, *Handshake age*, *Last command age*, *Telemetry age*,
*Group corrections*.

**Gruppo Finder `<codice>`** — un dispositivo per ogni pulsante/scenario
appreso (rinominalo, es. "Pulsante sala"):
- **Correzione automatica** — attiva/disattiva il recupero per il gruppo
- **Includi `<tapparella>`** — un interruttore per ogni membro
- **Correzioni**, **Osservazioni** (membri e codici negli attributi)
- **Dimentica gruppo** — azzera quanto appreso

---

## Le novità di questo fork

### Gateway 1Y.GU.005: sessione cloud stabile

Su questi impianti il cloud Finder non invia lo stato delle tapparelle
(`subscribe-plant` risponde `10 01 40 03`, `GetPlant` senza posizioni, nessuna
notifica push) e il gateway **resta legato alla prima sessione cloud**: dopo una
riconnessione i comandi vengono accettati ma non eseguiti finché il gateway non
viene riavviato. L'opzione **Sessione cloud stabile** (*Finder YOU →
Configura*) evita le riconnessioni inutili: niente ri-handshake prima dei
comandi, i timeout occasionali non chiudono la sessione, lettura ogni 120 s.

### ESP32 e mesh YESLY

- I frame di stato **rilanciati dalla mesh** non sono cifrati e contengono il MAC
  della tapparella d'origine: vengono decodificati, così **un solo ESP32** riceve
  lo stato anche delle tapparelle che non sente direttamente.
- Conferma BLE attesa **2 s** prima di ritentare da un altro ESP32 o passare al
  cloud; nel log tempo di conferma e strada (diretta/mesh) per ogni comando.
- Scelta dell'ESP32 **solo in base al segnale** (rimosse le regole scritte per la
  casa dell'autore originale), più la **mappa personalizzata**
  `/config/finder_you_mappa_personalizzata.yaml`, generata da sola, per indicare
  l'ESP32 da provare per primo per ogni tapparella.

### Recupero dei comandi di gruppo

I pulsanti fisici e gli scenari comandano più tapparelle con un unico frame
cifrato; capita che una tapparella con ricezione debole non lo riceva.
L'integrazione **impara da sola** quali tapparelle si muovono con ogni gruppo e
cosa significa ogni codice (apri/chiudi); quando un membro non parte entro
2,5 s nella direzione giusta gli manda il **comando singolo** (BLE, poi cloud).
Non interviene mai per codici sconosciuti, se nessun membro si è mosso, se la
tapparella è già al bersaglio o se nel frattempo arriva un nuovo comando.
Dettagli in [ROUTING.md](ROUTING.md).

---

## Installazione

1. HACS → ⋮ → **Repository personalizzati** →
   `https://github.com/l2jalbe/homeassistant-finder-you`, categoria **Integration**
2. Scarica **Finder YOU** e riavvia Home Assistant
3. *Impostazioni → Dispositivi e servizi → Aggiungi integrazione → Finder YOU*
4. Email e password dell'app Finder YOU — l'account deve essere
   **proprietario** dell'impianto; l'accesso con Google non è supportato (si può
   aggiungere una password all'account con "password dimenticata")
5. Gateway 1Y.GU.005: *Finder YOU → Configura* → **Sessione cloud stabile**

**Se i comandi cloud non rispondono (1Y.GU.005):** togli e rimetti
l'alimentazione al gateway, attendi 3 minuti, *Finder YOU → ⋮ → Ricarica*.
Evita di tenere aperta l'app Finder YOU: il cloud accetta un solo client attivo.

## ESP32 (consigliato)

Configurazione pronta nella cartella [`esphome/`](esphome/):

- [`finder-ble-1.yaml`](esphome/finder-ble-1.yaml) — nodo ESPHome (ESP32
  classico `esp32dev`, ESPHome 2026.10+), basato sul componente `finder_yesly`
  di [BLE_Finder_Yesly](https://github.com/francescodoffizi/BLE_Finder_Yesly)
- [`finder_relay.h`](esphome/finder_relay.h) — inoltro degli advertising a HA
  **senza il limite di 1 evento/s per MAC** del componente originale (i gateway
  rilanciano i frame di molte tapparelle con lo stesso MAC e ne andavano persi)
  e contatore dei comandi di gruppo

Passi: copia i due file in `/config/esphome/`, aggiungi ai secrets
`finder_ble_api_key`, `ota_password`, `ap_password` (oltre al Wi-Fi), primo
caricamento via USB (*Install → Manual download → Factory format*, poi
web.esphome.io), poi aggiornamenti via Wi-Fi. Nel dispositivo ESPHome in HA
attiva **"Allow the device to perform Home Assistant actions"**, altrimenti gli
stati non arrivano. Per un secondo ESP32 copia il file e cambia solo le righe
`substitutions`.

## Limiti noti

- Con gateway 1Y.GU.005 il cloud non fornisce gli stati: senza ESP32 HA conosce
  solo le posizioni richieste dai propri comandi.
- Le tapparelle trasmettono solo quando si muovono: segnale e ricezione si
  aggiornano con l'uso.
- Un ripetitore radio dei comandi sull'ESP32 è stato provato e non ha ridotto i
  comandi persi: il recupero è fatto da HA.
- Se la sessione cloud cade davvero (rete, riavvio di HA, app aperta) può
  servire riavviare il gateway. Un solo impianto per account.

## Documentazione

- [CHANGELOG.md](CHANGELOG.md) — modifiche versione per versione
- [ROUTING.md](ROUTING.md) — scelta dell'ESP32, mappa personalizzata, recupero
  dei gruppi, formato dei frame BLE
- [PROTOCOL.md](PROTOCOL.md) — protocollo cloud Finder (dal progetto originale)

## Crediti

- [7AC/homeassistant-finder-you](https://github.com/7AC/homeassistant-finder-you) — integrazione originale e reverse engineering del cloud
- [francescodoffizi](https://github.com/francescodoffizi) — BLE locale, luci, scenari e firmware ESP32
- [`hpack`](https://pypi.org/project/hpack/), [`condatek/finderblissha`](https://github.com/condatek/finderblissha)

Licenza MIT. Progetto non ufficiale, non affiliato a Finder S.p.A.; nomi e
marchi Finder, YESLY e Finder YOU appartengono ai rispettivi proprietari.
