// c3h_client.h - 32-bit side of the helper protocol. Copy kept in condor3-mod/src.
#ifndef C3H_CLIENT_H
#define C3H_CLIENT_H
#include <windows.h>
#include "c3h_proto.h"

typedef struct C3hClient { HANDLE map, req, done, proc; C3hHeader* hd; unsigned char* arena; } C3hClient;

// Creates the shared objects for this process, starts <exe> and waits until it serves (15 s). 0 on failure (err says why).
int  C3h_Start(C3hClient* c, const char* exe, const char* logPath, char* err, int errLen);
// One synchronous request; returns the answer's status (0 ok, 1 empty, < 0 error; -101 timeout, -102 helper died).
// The answer is c->arena[0 .. c->hd->resp_size) until the next request.
int  C3h_Request(C3hClient* c, unsigned type, int a0, int a1, const char* path, DWORD timeoutMs);
void C3h_Stop(C3hClient* c);
#endif
