#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // utilization 0-100 (5h window Pro/Max; spending % Enterprise)
    int session_reset_mins;  // minutes until reset
    float weekly_pct;        // 7-day utilization (Pro/Max only; 0 for Enterprise)
    int weekly_reset_mins;   // minutes until weekly reset (Pro/Max only)
    char status[16];         // "allowed", "limited", etc.
    bool chime;              // play the session-reset chime; false unless daemon opts in
    bool enterprise;         // true = Enterprise spending-limit account
    int time_pct;            // 0-100: fraction of billing period elapsed (Enterprise)
    int period_days;         // total billing period length in days (Enterprise)
    char reset_date[12];     // formatted reset date e.g. "Jul 1" (Enterprise)
    long clock_epoch;        // local wall-clock epoch (s) from daemon; 0 = not provided
    int  clock_fmt;          // 12 or 24 (hour format from daemon); defaults to 24
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse

    // Codex (OpenAI) usage — same two windows, read by the daemon straight out
    // of the Codex CLI's local session rollouts. codex_valid is false on a
    // Claude-only host (the daemon omits the keys), which keeps the original
    // Current/Weekly view; true splits the screen into Claude + Codex panels.
    float codex_session_pct;       // 5h window utilization 0-100
    int   codex_session_reset_mins;
    float codex_weekly_pct;        // 7d window utilization 0-100
    int   codex_weekly_reset_mins;
    bool  codex_valid;
};
