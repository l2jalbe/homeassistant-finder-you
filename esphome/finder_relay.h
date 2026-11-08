// =============================================================================
// finder_relay.h - estensioni locali per il nodo ESP32 Finder YESLY
// (va messo in /config/esphome/ accanto a finder-ble-1.yaml)
//
// forward_adv(): inoltra a Home Assistant i frame Finder (evento
//   esphome.finder_ble_adv). Sostituisce on_finder_adv_received() del
//   componente, che limitava a 1 evento/s per MAC: i gateway rilanciano i
//   frame di MOLTE tapparelle con lo stesso MAC e ne andavano persi.
//   Qui si scartano solo i frame IDENTICI dallo stesso mittente entro 2,5 s.
//
// count_command(): conta i comandi DIVERSI di pulsanti/scenari (frame da
//   15 byte) sentiti dall'avvio, ignorando le copie (raffica di
//   trasmissione, rilanci dei gateway). Solo diagnostica.
//
// Storia: la v2 conteneva anche un ripetitore dei comandi (ritrasmissione
// dei frame da 15 byte); nei test non ha ridotto i comandi persi ed e'
// stato rimosso. Il recupero dei comandi persi e' fatto dall'integrazione HA.
// =============================================================================
#pragma once

#include "esphome.h"
#include <cstdio>
#include <map>
#include <string>
#include <vector>

namespace finder_relay {

static bool g_forward_enabled = true;                  // switch "Finder Telemetry Forwarding"
static std::map<std::string, uint32_t> s_fwd_cache;    // mac|payload -> last ms
static std::map<std::string, uint32_t> s_cmd_seen;     // command key -> first seen ms
static uint32_t s_cmd_unique_count = 0;                // distinct command frames since boot

static const uint32_t FORWARD_DEDUP_MS = 2500;
static const uint32_t COMMAND_DEDUP_MS = 5000;
static const size_t COMMAND_FRAME_LEN = 15;

inline std::string to_hex(const std::vector<uint8_t> &d) {
  std::string out;
  char buf[4];
  for (uint8_t b : d) {
    snprintf(buf, sizeof(buf), "%02X ", b);
    out += buf;
  }
  return out;
}

template<typename M> inline void purge_older_than(M &m, uint32_t now, uint32_t age_ms) {
  if (m.size() < 64) return;
  for (auto it = m.begin(); it != m.end();) {
    if (now - it->second > age_ms) it = m.erase(it);
    else ++it;
  }
}

inline void forward_adv(const char *mac, int rssi, const std::vector<uint8_t> &d) {
#if defined(USE_API) && defined(USE_API_HOMEASSISTANT_SERVICES)
  if (!g_forward_enabled || d.empty()) return;
  if (esphome::api::global_api_server == nullptr || !esphome::api::global_api_server->is_connected()) return;
  const std::string hex = to_hex(d);
  const std::string key = std::string(mac) + "|" + hex;
  const uint32_t now = millis();
  auto it = s_fwd_cache.find(key);
  if (it != s_fwd_cache.end() && now - it->second < FORWARD_DEDUP_MS) return;
  s_fwd_cache[key] = now;
  purge_older_than(s_fwd_cache, now, 10000);
  esphome::api::CustomAPIDevice api_dev;
  api_dev.fire_homeassistant_event("esphome.finder_ble_adv",
                                   {{"mac", mac}, {"rssi", std::to_string(rssi)}, {"mf", hex}});
#endif
}

inline void count_command(const std::vector<uint8_t> &d) {
  if (d.size() != COMMAND_FRAME_LEN) return;
  // Key ignores the 0x40 "relayed" flag: direct and relayed copies count once.
  std::vector<uint8_t> k(d);
  k[0] &= 0xBF;
  const std::string key = to_hex(k);
  const uint32_t now = millis();
  auto it = s_cmd_seen.find(key);
  if (it != s_cmd_seen.end() && now - it->second < COMMAND_DEDUP_MS) return;
  s_cmd_seen[key] = now;
  purge_older_than(s_cmd_seen, now, 30000);
  s_cmd_unique_count++;
}

}  // namespace finder_relay
