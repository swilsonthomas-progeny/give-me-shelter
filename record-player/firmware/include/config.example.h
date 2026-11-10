// Copy this file to config.h and fill it in. config.h is not committed.
#pragma once

#define WIFI_SSID "your wifi name"        // 2.4 GHz network (the puck cannot see 5 GHz)
#define WIFI_PASS "your wifi password"

// The Raspberry Pi running turntable.py: its IP address, or its name ending in .local
#define SERVER_HOST "recordplayer.local"
#define SERVER_PORT 8888

// If one click of the ring moves several albums, or it takes several clicks to
// move one, change this (4, 2 or 1). Set ENC_REVERSE to 1 if it turns backwards.
#define ENC_COUNTS_PER_STEP 4
#define ENC_REVERSE 0
