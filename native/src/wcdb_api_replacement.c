/*
 * wcdb_api_replacement.c — drop-in replacement for the bundled wcdb_api.dll.
 *
 * Why this exists
 * ---------------
 * The original native/windows/wcdb_api.dll (built by WeFlow, 2026-05-07)
 * contains a hard-coded licence expiry in wcdb_init():
 *
 *     0x1800e4469  call _mktime64        ; 2026-09-30 23:59:59 UTC
 *     0x1800e447e  call _time64
 *     0x1800e4484  cmp  rax, rbx
 *     0x1800e45b6  mov  eax, 0xfffffc18  ; return -1000 once expired
 *
 * After 2026-09-30 every wcdb_init() call returns -1000 and webot cannot
 * start. This file reimplements the small C ABI that webot actually uses
 * (10 of the DLL's ~120 exports) on top of SQLCipher, so no expiry check
 * and no third-party licence is involved.
 *
 * The contract implemented here matches WeFlow's published
 * docs/third-party-components.md, which is the same ABI the old DLL spoke.
 *
 * Build: see tools/build_wcdb_api.py (gcc + SQLCipher amalgamation).
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sqlite3.h"
#include <openssl/evp.h>

#if defined(_WIN32)
#  define WCDB_EXPORT __declspec(dllexport)
#else
#  define WCDB_EXPORT
#endif

#define WCDB_OK            0
#define WCDB_ERR_ARG     (-1)
#define WCDB_ERR_KEY     (-2)
#define WCDB_ERR_OPEN    (-3)
#define WCDB_ERR_QUERY   (-4)
#define WCDB_ERR_MEM     (-5)

#define MAX_ACCOUNTS      16
#define MAX_PATH_LEN      1024
#define MAX_TABLE_CACHE   128

/* ── growable string buffer ─────────────────────────────────────────── */

typedef struct {
    char  *data;
    size_t len;
    size_t cap;
} strbuf;

static void sb_init(strbuf *sb) {
    sb->data = NULL;
    sb->len = 0;
    sb->cap = 0;
}

static int sb_reserve(strbuf *sb, size_t extra) {
    if (sb->len + extra + 1 <= sb->cap) {
        return 1;
    }
    size_t want = sb->cap ? sb->cap * 2 : 4096;
    while (want < sb->len + extra + 1) {
        want *= 2;
    }
    char *p = (char *)realloc(sb->data, want);
    if (!p) {
        return 0;
    }
    sb->data = p;
    sb->cap = want;
    return 1;
}

static int sb_addn(strbuf *sb, const char *s, size_t n) {
    if (!sb_reserve(sb, n)) {
        return 0;
    }
    memcpy(sb->data + sb->len, s, n);
    sb->len += n;
    sb->data[sb->len] = '\0';
    return 1;
}

static int sb_add(strbuf *sb, const char *s) {
    return s ? sb_addn(sb, s, strlen(s)) : 1;
}

static int sb_addc(strbuf *sb, char c) {
    return sb_addn(sb, &c, 1);
}

static int sb_printf(strbuf *sb, const char *fmt, ...) {
    char tmp[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n < 0) {
        return 0;
    }
    if ((size_t)n < sizeof(tmp)) {
        return sb_addn(sb, tmp, (size_t)n);
    }
    if (!sb_reserve(sb, (size_t)n)) {
        return 0;
    }
    va_start(ap, fmt);
    vsnprintf(sb->data + sb->len, (size_t)n + 1, fmt, ap);
    va_end(ap);
    sb->len += (size_t)n;
    return 1;
}

/* Emit a JSON string literal, escaping per RFC 8259. UTF-8 bytes pass through. */
static int sb_json_string(strbuf *sb, const char *s) {
    if (!sb_addc(sb, '"')) {
        return 0;
    }
    if (s) {
        for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
            switch (*p) {
            case '"':  if (!sb_add(sb, "\\\"")) return 0; break;
            case '\\': if (!sb_add(sb, "\\\\")) return 0; break;
            case '\b': if (!sb_add(sb, "\\b"))  return 0; break;
            case '\f': if (!sb_add(sb, "\\f"))  return 0; break;
            case '\n': if (!sb_add(sb, "\\n"))  return 0; break;
            case '\r': if (!sb_add(sb, "\\r"))  return 0; break;
            case '\t': if (!sb_add(sb, "\\t"))  return 0; break;
            default:
                if (*p < 0x20) {
                    if (!sb_printf(sb, "\\u%04x", (unsigned)*p)) return 0;
                } else {
                    if (!sb_addn(sb, (const char *)p, 1)) return 0;
                }
            }
        }
    }
    return sb_addc(sb, '"');
}

/* Hand ownership of the buffer to the caller (freed via wcdb_free_string). */
static void *sb_detach(strbuf *sb) {
    char *p = sb->data;
    sb->data = NULL;
    sb->len = sb->cap = 0;
    return p ? (void *)p : (void *)calloc(1, 1);
}

/* ── MD5 (for Msg_<md5(username)> table names) ──────────────────────── */

static int md5_hex(const char *in, char out[33]) {
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int dlen = 0;
    if (!EVP_Digest(in, strlen(in), digest, &dlen, EVP_md5(), NULL) || dlen != 16) {
        return 0;
    }
    static const char *hexd = "0123456789abcdef";
    for (unsigned i = 0; i < 16; i++) {
        out[i * 2]     = hexd[digest[i] >> 4];
        out[i * 2 + 1] = hexd[digest[i] & 0x0F];
    }
    out[32] = '\0';
    return 1;
}

/* ── account registry ───────────────────────────────────────────────── */

typedef struct {
    char table[128];
    char db[MAX_PATH_LEN];
} table_cache_entry;

typedef struct {
    int      used;
    int64_t  id;
    char     session_db[MAX_PATH_LEN];
    char     storage_root[MAX_PATH_LEN];
    char     contact_db[MAX_PATH_LEN];
    char     message_dir[MAX_PATH_LEN];
    char     key[256];
    sqlite3 *contact;
    table_cache_entry tables[MAX_TABLE_CACHE];
    int      table_count;
} account;

static account   g_accounts[MAX_ACCOUNTS];
static int64_t   g_next_id = 1;
static CRITICAL_SECTION g_lock;
static LONG      g_lock_ready = 0;

static void ensure_lock(void) {
    if (InterlockedCompareExchange(&g_lock_ready, 1, 0) == 0) {
        InitializeCriticalSection(&g_lock);
    }
}

static account *find_account(int64_t id) {
    for (int i = 0; i < MAX_ACCOUNTS; i++) {
        if (g_accounts[i].used && g_accounts[i].id == id) {
            return &g_accounts[i];
        }
    }
    return NULL;
}

/* ── path helpers ───────────────────────────────────────────────────── */

static void path_dirname(const char *path, char *out, size_t outlen) {
    const char *slash = strrchr(path, '\\');
    const char *fwd   = strrchr(path, '/');
    if (fwd > slash) {
        slash = fwd;
    }
    if (!slash) {
        snprintf(out, outlen, ".");
        return;
    }
    size_t n = (size_t)(slash - path);
    if (n >= outlen) {
        n = outlen - 1;
    }
    memcpy(out, path, n);
    out[n] = '\0';
}

/* session.db lives at <storage_root>/session/session.db */
static void derive_paths(account *a, const char *session_db) {
    snprintf(a->session_db, sizeof(a->session_db), "%s", session_db);
    char session_dir[MAX_PATH_LEN];
    path_dirname(session_db, session_dir, sizeof(session_dir));
    path_dirname(session_dir, a->storage_root, sizeof(a->storage_root));
    snprintf(a->contact_db, sizeof(a->contact_db), "%s\\contact\\contact.db", a->storage_root);
    snprintf(a->message_dir, sizeof(a->message_dir), "%s\\message", a->storage_root);
}

static int file_exists(const char *p) {
    DWORD attrs = GetFileAttributesA(p);
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

/* ── SQLCipher open ─────────────────────────────────────────────────── */

static int is_hex64(const char *s) {
    if (!s || strlen(s) != 64) {
        return 0;
    }
    for (const char *p = s; *p; p++) {
        if (!isxdigit((unsigned char)*p)) {
            return 0;
        }
    }
    return 1;
}

static int hex_to_bytes(const char *hex, unsigned char *out, size_t out_len) {
    if (!is_hex64(hex) || out_len != 32) {
        return 0;
    }
    for (size_t i = 0; i < out_len; i++) {
        int hi, lo;
        char a = hex[i * 2], b = hex[i * 2 + 1];
        if (a >= '0' && a <= '9')      hi = a - '0';
        else if (a >= 'a' && a <= 'f') hi = a - 'a' + 10;
        else if (a >= 'A' && a <= 'F') hi = a - 'A' + 10;
        else return 0;
        if (b >= '0' && b <= '9')      lo = b - '0';
        else if (b >= 'a' && b <= 'f') lo = b - 'a' + 10;
        else if (b >= 'A' && b <= 'F') lo = b - 'A' + 10;
        else return 0;
        out[i] = (unsigned char)((hi << 4) | lo);
    }
    return 1;
}

/* Key delivery forms, tried in order until one decrypts.
 *
 * This ordering is not cosmetic.  WeChat hands the 32 raw key bytes to
 * sqlite3_key(), which SQLCipher treats as a PASSPHRASE and runs through
 * PBKDF2 — it does NOT use them as a raw key.  Reading the 64-hex string as
 * `PRAGMA key = "x'...'"` (raw-key form) derives a different key and every
 * legitimate WeChat database is rejected with SQLITE_NOTADB.  Measured on a
 * real 4.1.9 install: only the passphrase-over-32-bytes form decrypts
 * session.db / contact.db / message_0.db.
 */
#define KEYFORM_RAW32      0   /* sqlite3_key(db, raw 32 bytes, 32) — WeChat */
#define KEYFORM_RAWKEY     1   /* PRAGMA key = "x'<hex>'"  — raw-key form */
#define KEYFORM_HEXPASS    2   /* PRAGMA key = '<hex>'     — hex as passphrase */
#define KEYFORM_PASSPHRASE 3   /* PRAGMA key = '<text>'    — any other key */

static sqlite3 *open_with_form(const char *path, const char *key,
                               const unsigned char *raw32, int form) {
    sqlite3 *db = NULL;
    if (sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, NULL) != SQLITE_OK) {
        if (db) {
            sqlite3_close(db);
        }
        return NULL;
    }

    if (form == KEYFORM_RAW32) {
        sqlite3_key(db, raw32, 32);
    } else {
        char pragma[1024];
        if (form == KEYFORM_RAWKEY) {
            snprintf(pragma, sizeof(pragma), "PRAGMA key = \"x'%s'\";", key);
        } else {
            /* Escape single quotes so an odd key cannot break out of the
               literal. */
            char escaped[768];
            size_t j = 0;
            for (const char *p = key ? key : ""; *p && j + 3 < sizeof(escaped); p++) {
                if (*p == '\'') {
                    escaped[j++] = '\'';
                }
                escaped[j++] = *p;
            }
            escaped[j] = '\0';
            snprintf(pragma, sizeof(pragma), "PRAGMA key = '%s';", escaped);
        }
        if (sqlite3_exec(db, pragma, NULL, NULL, NULL) != SQLITE_OK) {
            sqlite3_close(db);
            return NULL;
        }
    }

    /* Verify the key really decrypts before trusting the handle. */
    sqlite3_stmt *st = NULL;
    if (sqlite3_prepare_v2(db, "SELECT count(*) FROM sqlite_master;", -1, &st, NULL) != SQLITE_OK) {
        sqlite3_close(db);
        return NULL;
    }
    int ok = (sqlite3_step(st) == SQLITE_ROW);
    sqlite3_finalize(st);
    if (!ok) {
        sqlite3_close(db);
        return NULL;
    }
    return db;
}

static sqlite3 *open_encrypted(const char *path, const char *key) {
    if (!file_exists(path)) {
        return NULL;
    }

    unsigned char raw32[32];
    int have_raw32 = hex_to_bytes(key, raw32, sizeof(raw32));

    /* WeChat's own form first, then the compatibility fallbacks. */
    if (have_raw32) {
        sqlite3 *db = open_with_form(path, key, raw32, KEYFORM_RAW32);
        if (db) {
            return db;
        }
        db = open_with_form(path, key, raw32, KEYFORM_RAWKEY);
        if (db) {
            return db;
        }
        return open_with_form(path, key, raw32, KEYFORM_HEXPASS);
    }

    return open_with_form(path, key, raw32, KEYFORM_PASSPHRASE);
}

static int table_exists(sqlite3 *db, const char *name) {
    sqlite3_stmt *st = NULL;
    if (sqlite3_prepare_v2(db,
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1 LIMIT 1;",
            -1, &st, NULL) != SQLITE_OK) {
        return 0;
    }
    sqlite3_bind_text(st, 1, name, -1, SQLITE_TRANSIENT);
    int found = (sqlite3_step(st) == SQLITE_ROW);
    sqlite3_finalize(st);
    return found;
}

/* Find which message_*.db holds Msg_<md5>, caching the answer. */
static const char *find_message_db(account *a, const char *table) {
    for (int i = 0; i < a->table_count; i++) {
        if (strcmp(a->tables[i].table, table) == 0) {
            return a->tables[i].db[0] ? a->tables[i].db : NULL;
        }
    }

    char pattern[MAX_PATH_LEN];
    snprintf(pattern, sizeof(pattern), "%s\\*.db", a->message_dir);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pattern, &fd);
    char found[MAX_PATH_LEN] = "";

    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                continue;
            }
            char candidate[MAX_PATH_LEN];
            snprintf(candidate, sizeof(candidate), "%s\\%s", a->message_dir, fd.cFileName);
            sqlite3 *db = open_encrypted(candidate, a->key);
            if (!db) {
                continue;
            }
            if (table_exists(db, table)) {
                snprintf(found, sizeof(found), "%s", candidate);
            }
            sqlite3_close(db);
            if (found[0]) {
                break;
            }
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }

    /* Store the result in the cache and return the cached copy — returning
     * the local buffer would hand the caller a dangling pointer. */
    if (a->table_count < MAX_TABLE_CACHE) {
        table_cache_entry *e = &a->tables[a->table_count++];
        snprintf(e->table, sizeof(e->table), "%s", table);
        snprintf(e->db, sizeof(e->db), "%s", found);
        return e->db[0] ? e->db : NULL;
    }
    return NULL;
}

/* ── generic row -> JSON ────────────────────────────────────────────── */

static int emit_column(strbuf *sb, sqlite3_stmt *st, int i) {
    switch (sqlite3_column_type(st, i)) {
    case SQLITE_NULL:
        return sb_add(sb, "null");
    case SQLITE_INTEGER:
        return sb_printf(sb, "%lld", (long long)sqlite3_column_int64(st, i));
    case SQLITE_FLOAT:
        return sb_printf(sb, "%.17g", sqlite3_column_double(st, i));
    default: {
        const unsigned char *t = sqlite3_column_text(st, i);
        return sb_json_string(sb, t ? (const char *)t : "");
    }
    }
}

/* ── JSON array-of-strings parser (for usernames inputs) ────────────── */

typedef struct {
    char **items;
    int    count;
} str_list;

static void str_list_free(str_list *l) {
    for (int i = 0; i < l->count; i++) {
        free(l->items[i]);
    }
    free(l->items);
    l->items = NULL;
    l->count = 0;
}

static int str_list_parse(const char *json, str_list *out) {
    out->items = NULL;
    out->count = 0;
    if (!json) {
        return 1;
    }
    const char *p = json;
    while (*p && *p != '[') {
        p++;
    }
    if (*p != '[') {
        return 1; /* not an array -> empty list */
    }
    p++;
    int cap = 0;
    while (*p) {
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',') {
            p++;
        }
        if (*p == ']' || !*p) {
            break;
        }
        if (*p != '"') {
            /* skip non-string elements */
            while (*p && *p != ',' && *p != ']') {
                p++;
            }
            continue;
        }
        p++;
        strbuf item;
        sb_init(&item);
        while (*p && *p != '"') {
            if (*p == '\\' && p[1]) {
                p++;
                switch (*p) {
                case 'n': sb_addc(&item, '\n'); break;
                case 't': sb_addc(&item, '\t'); break;
                case 'r': sb_addc(&item, '\r'); break;
                case 'b': sb_addc(&item, '\b'); break;
                case 'f': sb_addc(&item, '\f'); break;
                default:  sb_addc(&item, *p);   break;
                }
            } else {
                sb_addc(&item, *p);
            }
            p++;
        }
        if (*p == '"') {
            p++;
        }
        if (out->count == cap) {
            cap = cap ? cap * 2 : 16;
            char **grown = (char **)realloc(out->items, sizeof(char *) * (size_t)cap);
            if (!grown) {
                free(item.data);
                str_list_free(out);
                return 0;
            }
            out->items = grown;
        }
        out->items[out->count++] = item.data ? item.data : _strdup("");
    }
    return 1;
}

/* ── exports ────────────────────────────────────────────────────────── */

/*
 * The old DLL ran a licence check here and returned -101 once expired.
 * This replacement has nothing to protect, so it always reports success.
 */
WCDB_EXPORT int32_t InitProtection(const char *resource_path) {
    (void)resource_path;
    return WCDB_OK;
}

/*
 * Marker so callers can detect the replacement and skip byte patching of
 * the old DLL's DRM flag (the patch RVA does not exist in this image).
 */
WCDB_EXPORT const char *webot_wcdb_api_replacement(void) {
    return "webot-wcdb-api-replacement/1.0 (SQLCipher)";
}

WCDB_EXPORT int32_t wcdb_init(void) {
    ensure_lock();
    return WCDB_OK;
}

WCDB_EXPORT int32_t wcdb_shutdown(void) {
    ensure_lock();
    EnterCriticalSection(&g_lock);
    for (int i = 0; i < MAX_ACCOUNTS; i++) {
        if (g_accounts[i].used) {
            if (g_accounts[i].contact) {
                sqlite3_close(g_accounts[i].contact);
                g_accounts[i].contact = NULL;
            }
            g_accounts[i].used = 0;
        }
    }
    LeaveCriticalSection(&g_lock);
    return WCDB_OK;
}

WCDB_EXPORT int32_t wcdb_open_account(const char *path, const char *key, int64_t *out_handle) {
    ensure_lock();
    if (!path || !out_handle) {
        return WCDB_ERR_ARG;
    }
    if (!key || !*key) {
        return WCDB_ERR_KEY;
    }

    /* Validate the key against session.db before registering the account. */
    sqlite3 *probe = open_encrypted(path, key);
    if (!probe) {
        return WCDB_ERR_OPEN;
    }
    sqlite3_close(probe);

    EnterCriticalSection(&g_lock);
    account *slot = NULL;
    for (int i = 0; i < MAX_ACCOUNTS; i++) {
        if (!g_accounts[i].used) {
            slot = &g_accounts[i];
            break;
        }
    }
    if (!slot) {
        LeaveCriticalSection(&g_lock);
        return WCDB_ERR_MEM;
    }
    memset(slot, 0, sizeof(*slot));
    slot->used = 1;
    slot->id = g_next_id++;
    snprintf(slot->key, sizeof(slot->key), "%s", key);
    derive_paths(slot, path);
    *out_handle = slot->id;
    LeaveCriticalSection(&g_lock);
    return WCDB_OK;
}

WCDB_EXPORT int32_t wcdb_close_account(int64_t handle) {
    ensure_lock();
    EnterCriticalSection(&g_lock);
    account *a = find_account(handle);
    if (a) {
        if (a->contact) {
            sqlite3_close(a->contact);
            a->contact = NULL;
        }
        a->used = 0;
    }
    LeaveCriticalSection(&g_lock);
    return WCDB_OK;
}

WCDB_EXPORT void wcdb_free_string(void *ptr) {
    free(ptr);
}

/* SELECT * FROM SessionTable ORDER BY sort_timestamp DESC, last_timestamp DESC */
WCDB_EXPORT int32_t wcdb_get_sessions(int64_t handle, void **out_json) {
    ensure_lock();
    if (!out_json) {
        return WCDB_ERR_ARG;
    }
    *out_json = NULL;

    EnterCriticalSection(&g_lock);
    account *a = find_account(handle);
    LeaveCriticalSection(&g_lock);
    if (!a) {
        return WCDB_ERR_ARG;
    }

    sqlite3 *db = open_encrypted(a->session_db, a->key);
    if (!db) {
        return WCDB_ERR_OPEN;
    }

    const char *sql =
        "SELECT * FROM SessionTable "
        "ORDER BY sort_timestamp DESC, last_timestamp DESC;";

    sqlite3_stmt *st = NULL;
    if (sqlite3_prepare_v2(db, sql, -1, &st, NULL) != SQLITE_OK) {
        /* Older/variant schemas may lack sort_timestamp. */
        if (sqlite3_prepare_v2(db, "SELECT * FROM SessionTable;", -1, &st, NULL) != SQLITE_OK) {
            sqlite3_close(db);
            return WCDB_ERR_QUERY;
        }
    }

    strbuf sb;
    sb_init(&sb);
    sb_add(&sb, "{\"sessions\":[");
    int rows = 0;
    while (sqlite3_step(st) == SQLITE_ROW) {
        if (rows++) {
            sb_addc(&sb, ',');
        }
        sb_addc(&sb, '{');
        int cols = sqlite3_column_count(st);
        for (int i = 0; i < cols; i++) {
            if (i) {
                sb_addc(&sb, ',');
            }
            sb_json_string(&sb, sqlite3_column_name(st, i));
            sb_addc(&sb, ':');
            emit_column(&sb, st, i);
        }
        sb_addc(&sb, '}');
    }
    sb_printf(&sb, "],\"count\":%d}", rows);

    sqlite3_finalize(st);
    sqlite3_close(db);
    *out_json = sb_detach(&sb);
    return WCDB_OK;
}

/*
 * Messages for one chat. Table name is Msg_<md5(talker)>; the sender wxid
 * comes from the per-database Name2Id/name2id mapping on real_sender_id.
 */
WCDB_EXPORT int32_t wcdb_get_messages(int64_t handle, const char *talker,
                                      int32_t limit, int32_t offset, void **out_json) {
    ensure_lock();
    if (!out_json || !talker) {
        return WCDB_ERR_ARG;
    }
    *out_json = NULL;

    EnterCriticalSection(&g_lock);
    account *a = find_account(handle);
    LeaveCriticalSection(&g_lock);
    if (!a) {
        return WCDB_ERR_ARG;
    }

    char digest[33];
    if (!md5_hex(talker, digest)) {
        return WCDB_ERR_QUERY;
    }
    char table[64];
    snprintf(table, sizeof(table), "Msg_%s", digest);

    const char *db_path = find_message_db(a, table);
    if (!db_path) {
        /* No table for this chat yet — empty result is not an error. */
        strbuf empty;
        sb_init(&empty);
        sb_add(&empty, "{\"messages\":[],\"count\":0}");
        *out_json = sb_detach(&empty);
        return WCDB_OK;
    }

    sqlite3 *db = open_encrypted(db_path, a->key);
    if (!db) {
        return WCDB_ERR_OPEN;
    }

    /* Resolve the sender table name variant present in this database. */
    const char *n2i = table_exists(db, "Name2Id") ? "Name2Id"
                    : table_exists(db, "name2id") ? "name2id"
                    : NULL;

    strbuf sql;
    sb_init(&sql);
    sb_add(&sql,
        "SELECT m.local_id AS local_id, m.server_id AS server_id, "
        "m.local_type AS localType, m.sort_seq AS sort_seq, "
        "m.create_time AS create_time, m.status AS status, "
        "m.message_content AS message_content, m.source AS source, ");
    if (n2i) {
        sb_printf(&sql, "n.user_name AS sender_username ");
        sb_printf(&sql, "FROM \"%s\" m LEFT JOIN %s n ON n.rowid = m.real_sender_id ", table, n2i);
    } else {
        sb_add(&sql, "'' AS sender_username ");
        sb_printf(&sql, "FROM \"%s\" m ", table);
    }
    sb_add(&sql, "ORDER BY m.create_time DESC, m.local_id DESC LIMIT ?1 OFFSET ?2;");

    sqlite3_stmt *st = NULL;
    if (sqlite3_prepare_v2(db, sql.data, -1, &st, NULL) != SQLITE_OK) {
        free(sql.data);
        sqlite3_close(db);
        return WCDB_ERR_QUERY;
    }
    free(sql.data);

    sqlite3_bind_int(st, 1, limit > 0 ? limit : 200);
    sqlite3_bind_int(st, 2, offset > 0 ? offset : 0);

    strbuf out;
    sb_init(&out);
    sb_add(&out, "{\"messages\":[");
    int rows = 0;
    while (sqlite3_step(st) == SQLITE_ROW) {
        if (rows++) {
            sb_addc(&out, ',');
        }
        sb_addc(&out, '{');
        int cols = sqlite3_column_count(st);
        for (int i = 0; i < cols; i++) {
            if (i) {
                sb_addc(&out, ',');
            }
            sb_json_string(&out, sqlite3_column_name(st, i));
            sb_addc(&out, ':');
            emit_column(&out, st, i);
        }
        sb_addc(&out, '}');
    }
    sb_printf(&out, "],\"count\":%d}", rows);

    sqlite3_finalize(st);
    sqlite3_close(db);
    *out_json = sb_detach(&out);
    return WCDB_OK;
}

/* {wxid: displayName, ...} — display name prefers remark over nickname. */
WCDB_EXPORT int32_t wcdb_get_display_names(int64_t handle, const char *usernames_json,
                                           void **out_json) {
    ensure_lock();
    if (!out_json) {
        return WCDB_ERR_ARG;
    }
    *out_json = NULL;

    EnterCriticalSection(&g_lock);
    account *a = find_account(handle);
    LeaveCriticalSection(&g_lock);
    if (!a) {
        return WCDB_ERR_ARG;
    }

    str_list names;
    if (!str_list_parse(usernames_json, &names)) {
        return WCDB_ERR_MEM;
    }

    strbuf out;
    sb_init(&out);
    sb_add(&out, "{");

    sqlite3 *db = open_encrypted(a->contact_db, a->key);
    int first = 1;
    for (int i = 0; i < names.count; i++) {
        const char *wxid = names.items[i];
        const char *display = wxid;
        /* sqlite3_column_text() points into the statement's own buffer, which
         * sqlite3_finalize() invalidates.  Copy before finalizing. */
        char display_buf[512];
        display_buf[0] = '\0';

        if (db) {
            sqlite3_stmt *st = NULL;
            const char *sql =
                "SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), "
                "NULLIF(nickName,''), NULLIF(alias,''), NULLIF(display_name,'')) "
                "FROM contact WHERE username = ?1 LIMIT 1;";
            if (sqlite3_prepare_v2(db, sql, -1, &st, NULL) == SQLITE_OK) {
                sqlite3_bind_text(st, 1, wxid, -1, SQLITE_TRANSIENT);
                if (sqlite3_step(st) == SQLITE_ROW) {
                    const unsigned char *t = sqlite3_column_text(st, 0);
                    if (t && *t) {
                        snprintf(display_buf, sizeof(display_buf), "%s", (const char *)t);
                    }
                }
                sqlite3_finalize(st);
            } else {
                /* Fall back to a schema with only nick_name. */
                if (sqlite3_prepare_v2(db,
                        "SELECT COALESCE(NULLIF(remark,''), NULLIF(nick_name,'')) "
                        "FROM contact WHERE username = ?1 LIMIT 1;",
                        -1, &st, NULL) == SQLITE_OK) {
                    sqlite3_bind_text(st, 1, wxid, -1, SQLITE_TRANSIENT);
                    if (sqlite3_step(st) == SQLITE_ROW) {
                        const unsigned char *t = sqlite3_column_text(st, 0);
                        if (t && *t) {
                            snprintf(display_buf, sizeof(display_buf), "%s", (const char *)t);
                        }
                    }
                    sqlite3_finalize(st);
                }
            }
        }

        if (display_buf[0]) {
            display = display_buf;
        }

        if (!first) {
            sb_addc(&out, ',');
        }
        first = 0;
        sb_json_string(&out, wxid);
        sb_addc(&out, ':');
        sb_json_string(&out, display);
    }
    sb_add(&out, "}");

    if (db) {
        sqlite3_close(db);
    }
    str_list_free(&names);
    *out_json = sb_detach(&out);
    return WCDB_OK;
}

/* Contacts, optionally filtered by the single keyword the caller passes. */
WCDB_EXPORT int32_t wcdb_get_contacts_compact(int64_t handle, const char *usernames_json,
                                              void **out_json) {
    ensure_lock();
    if (!out_json) {
        return WCDB_ERR_ARG;
    }
    *out_json = NULL;

    EnterCriticalSection(&g_lock);
    account *a = find_account(handle);
    LeaveCriticalSection(&g_lock);
    if (!a) {
        return WCDB_ERR_ARG;
    }

    str_list filter;
    if (!str_list_parse(usernames_json, &filter)) {
        return WCDB_ERR_MEM;
    }
    const char *keyword = (filter.count > 0) ? filter.items[0] : "";

    strbuf out;
    sb_init(&out);
    sb_add(&out, "{\"contacts\":[");

    sqlite3 *db = open_encrypted(a->contact_db, a->key);
    int rows = 0;
    if (db) {
        sqlite3_stmt *st = NULL;
        const char *sql =
            "SELECT username, "
            "COALESCE(NULLIF(remark,''), NULLIF(nick_name,''), username) AS display, "
            "COALESCE(remark,'') AS remark, "
            "COALESCE(nick_name,'') AS nick_name, "
            "COALESCE(alias,'') AS alias "
            "FROM contact "
            "WHERE username IS NOT NULL AND username != '' "
            "AND (?1 = '' OR username LIKE ?2 OR remark LIKE ?2 OR nick_name LIKE ?2) "
            "ORDER BY username LIMIT 1000;";
        if (sqlite3_prepare_v2(db, sql, -1, &st, NULL) == SQLITE_OK) {
            char like[512];
            snprintf(like, sizeof(like), "%%%s%%", keyword);
            sqlite3_bind_text(st, 1, keyword, -1, SQLITE_TRANSIENT);
            sqlite3_bind_text(st, 2, like, -1, SQLITE_TRANSIENT);
            while (sqlite3_step(st) == SQLITE_ROW) {
                if (rows++) {
                    sb_addc(&out, ',');
                }
                sb_addc(&out, '{');
                int cols = sqlite3_column_count(st);
                for (int i = 0; i < cols; i++) {
                    if (i) {
                        sb_addc(&out, ',');
                    }
                    sb_json_string(&out, sqlite3_column_name(st, i));
                    sb_addc(&out, ':');
                    emit_column(&out, st, i);
                }
                sb_addc(&out, '}');
            }
            sqlite3_finalize(st);
        }
        sqlite3_close(db);
    }
    sb_printf(&out, "],\"count\":%d}", rows);

    str_list_free(&filter);
    *out_json = sb_detach(&out);
    return WCDB_OK;
}

/* Members of a chatroom, resolved through name2id + contact. */
WCDB_EXPORT int32_t wcdb_get_group_members(int64_t handle, const char *chatroom_id,
                                           void **out_json) {
    ensure_lock();
    if (!out_json || !chatroom_id) {
        return WCDB_ERR_ARG;
    }
    *out_json = NULL;

    EnterCriticalSection(&g_lock);
    account *a = find_account(handle);
    LeaveCriticalSection(&g_lock);
    if (!a) {
        return WCDB_ERR_ARG;
    }

    strbuf out;
    sb_init(&out);
    sb_add(&out, "{\"members\":[");
    int rows = 0;

    sqlite3 *db = open_encrypted(a->contact_db, a->key);
    if (db && table_exists(db, "chatroom_member") && table_exists(db, "name2id")) {
        sqlite3_stmt *st = NULL;
        const char *sql =
            "SELECT n.username AS username, "
            "COALESCE(c.small_head_url, '') AS avatarUrl "
            "FROM chatroom_member m "
            "JOIN name2id n ON n.rowid = m.member_id "
            "LEFT JOIN contact c ON c.username = n.username "
            "WHERE m.room_id = (SELECT rowid FROM name2id WHERE username = ?1 LIMIT 1);";
        if (sqlite3_prepare_v2(db, sql, -1, &st, NULL) == SQLITE_OK) {
            sqlite3_bind_text(st, 1, chatroom_id, -1, SQLITE_TRANSIENT);
            while (sqlite3_step(st) == SQLITE_ROW) {
                if (rows++) {
                    sb_addc(&out, ',');
                }
                sb_addc(&out, '{');
                int cols = sqlite3_column_count(st);
                for (int i = 0; i < cols; i++) {
                    if (i) {
                        sb_addc(&out, ',');
                    }
                    sb_json_string(&out, sqlite3_column_name(st, i));
                    sb_addc(&out, ':');
                    emit_column(&out, st, i);
                }
                sb_addc(&out, '}');
            }
            sqlite3_finalize(st);
        }
    }
    if (db) {
        sqlite3_close(db);
    }

    sb_printf(&out, "],\"count\":%d}", rows);
    *out_json = sb_detach(&out);
    return WCDB_OK;
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved) {
    (void)module;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        ensure_lock();
    }
    return TRUE;
}
