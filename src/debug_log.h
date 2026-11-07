#ifndef DEBUG_LOG_H
#define DEBUG_LOG_H

#ifdef __cplusplus
extern "C" {
#endif

// Port, auf dem der Monitor lauscht. Bei Bedarf per -DDBGLOG_PORT=... überschreiben.
#ifndef DBGLOG_PORT
#define DBGLOG_PORT 28734
#endif

#ifdef NDEBUG
// Release-Build: Init/Shutdown/Write werden komplett wegoptimiert.
static inline void DbgLogInit(void)     {}
static inline void DbgLogShutdown(void) {}

// Wichtig: KEINE Funktion für DbgLogWrite aufrufen (auch nicht leer),
// sonst werden die Argumente trotzdem ausgewertet (z.B. teure Berechnungen
// in den Argumenten). Stattdessen der ganze Ausdruck weg via Makro.
#define DbgLogWrite(...) ((void)0)

#else
// Initialisiert Winsock. Muss einmal aufgerufen werden (z.B. in DllMain / DLL_PROCESS_ATTACH).
// Baut NICHT sofort eine Verbindung auf (lazy connect beim ersten Log).
void DbgLogInit(void);

// Schließt Socket & räumt Winsock auf (z.B. in DLL_PROCESS_DETACH).
void DbgLogShutdown(void);

// Sendet eine formatierte Log-Zeile an den Monitor, falls verbunden.
// Reconnect-Versuche sind rate-limited, damit es die Ziel-App nicht ausbremst
// falls kein Monitor läuft. Niemals blockierend über längere Zeit.
void DbgLogWrite(const char* fmt, ...);

#endif // NDEBUG

// Bequemlichkeits-Makro
#define DBGLOG(...) DbgLogWrite(__VA_ARGS__)

#ifdef __cplusplus
}
#endif

#endif // DEBUG_LOG_H