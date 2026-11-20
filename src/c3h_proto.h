// c3h_proto.h - shared-memory protocol between the 32-bit Condor 3 mod (d3dx.dll) and the 64-bit helper (c3helper.exe).
// Both sides include this file; keep it free of pointers and of types whose size differs between 32 and 64 bit.
// A copy lives in condor3-mod/src/c3h_proto.h: keep both identical (C3H_VERSION is checked at runtime).
//
// Objects (all in the same CrossOver bottle / Windows session, <pid> = process id of Condor.exe, decimal):
//   file mapping  Local\c3h_<pid>_shm    C3hHeader at offset 0, arena at offset C3H_HEADER_SIZE
//   event         Local\c3h_<pid>_req    auto-reset: the mod signals it after writing a request
//   event         Local\c3h_<pid>_done   auto-reset: the helper signals it when the answer is complete
// One request at a time: the mod writes req_*, increments req_seq, signals _req and waits for _done with done_seq == req_seq.
#ifndef C3H_PROTO_H
#define C3H_PROTO_H
#include <stdint.h>

#define C3H_MAGIC        0x48334343u     /* 'CC3H' */
#define C3H_VERSION      1u
#define C3H_HEADER_SIZE  4096u
#define C3H_ARENA_SIZE   (48u << 20)     /* answer buffer; also limits the size of one chunk / texture */

enum {
    C3H_REQ_OPEN    = 1,   // req_path = landscape folder; answer = C3hLandscape + C3hChunkInfo[n_chunks]
    C3H_REQ_CHUNK   = 2,   // req_arg = {chunk index, lod};  answer = C3hChunk + C3hPart[] + vertices + indices
    C3H_REQ_TEXTURE = 3,   // req_arg = {texture id, lod};   answer = a complete .dds file (lod = mip levels dropped)
    C3H_REQ_STATS   = 4,   // answer: text in resp_msg
    C3H_REQ_QUIT    = 5
};

#pragma pack(push, 4)
typedef struct C3hHeader {
    uint32_t magic, version, header_size, arena_size;
    volatile uint32_t helper_ready;     // helper: 1 = serving requests
    volatile uint32_t req_seq;          // mod: incremented after a request was written
    volatile uint32_t done_seq;         // helper: set to req_seq when the answer is complete
    uint32_t req_type;
    int32_t  req_arg[4];
    char     req_path[512];             // OPEN: landscape folder as a Windows path (e.g. C:\Condor3\Landscapes\OrlyDemo)
    int32_t  status;                    // 0 = ok, 1 = ok but empty, < 0 = error (resp_msg says why)
    uint32_t resp_size;                 // valid bytes in the arena
    char     resp_msg[512];
} C3hHeader;

#define C3H_MAX_LOD 3

typedef struct C3hLandscape {
    uint32_t tiles_x, tiles_y;          // landscape size in tiles (from the .trn header)
    float    tile_size;                 // metres (64 * cell size)
    uint32_t chunks_per_tile;           // chunks along one tile side
    uint32_t n_chunks;                  // C3hChunkInfo entries that follow
    uint32_t n_textures;
    uint32_t n_lods;                    // LOD levels available for every chunk (0 = full detail)
    float    lod_cell[C3H_MAX_LOD];     // vertex clustering cell size per LOD (0 for LOD 0)
} C3hLandscape;

typedef struct C3hChunkInfo {
    int16_t  tile_x, tile_y;            // Condor tile (x counts west, y north)
    uint16_t cx, cy;                    // chunk within the tile
    float    min[3], max[3];            // AABB in the tile-centred frame of the vertices (x west, y north, z = altitude)
    uint32_t bytes[C3H_MAX_LOD];        // answer size per LOD (0 = empty)
    uint32_t tris[C3H_MAX_LOD];
} C3hChunkInfo;

typedef struct C3hChunk {
    int16_t  tile_x, tile_y;
    uint32_t lod;
    uint32_t n_parts, n_verts, n_indices;
} C3hChunk;                             // followed by C3hPart[n_parts], C3hVertex[n_verts], uint32_t[n_indices]

typedef struct C3hPart {
    uint32_t first_vertex, vertex_count, first_index, index_count;   // indices are absolute vertex numbers of the chunk
    int32_t  texture;                   // texture id (C3H_REQ_TEXTURE), -1 = none
    float    color[4];
    float    params[3];                 // spec, shiny, env
    char     name[32];                  // object name (ShortString content, NUL terminated)
} C3hPart;

typedef struct C3hVertex { float pos[3], normal[3], uv[2]; } C3hVertex;   // = C3D vertex (32 bytes)
#pragma pack(pop)

#endif
