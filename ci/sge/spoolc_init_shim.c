/* ci/sge: make Debian's dlopen'd libspoolc.so usable for classic spooling.
 *
 * Debian/Ubuntu link the SoGE uti/sgeobj/spool framework modules into BOTH
 * sge_qmaster and the dlopen'd libspoolc.so, with -Bsymbolic-functions
 * pinning each copy's internal calls to itself. Three failure layers stack
 * on the duplicated static state, all fixed here:
 *
 * 1. libspoolc's *_mt_init are never called: its pthread keys stay 0 and
 *    alias foreign keys -> SIGSEGV in bootstrap_get_ignore_fqdn / the
 *    obj-state validate path. Fix: constructor calls each *_mt_init via
 *    dlsym on libspoolc's own handle (bypasses the exe's duplicates).
 * 2. The default spool context global is per-copy; the exe sets its own,
 *    libspoolc's stays NULL -> CULL abort ("got NULL element") in the
 *    SGE_TYPE_CQUEUE read. Fix: opendir() interposer copies the exe's
 *    context into libspoolc's copy once both exist.
 * 3. The master object lists are per-copy; libspoolc's stay empty, so
 *    qinstance validation resolves against nothing and every queue
 *    instance is silently dropped on spool read. Fix: the same interposer
 *    aliases the exe's master-list pointers into libspoolc's slots
 *    (types 0..30, SGE_TYPE_ALL == 31 in 8.1.9). The aliased lists are
 *    never freed by libspoolc before process teardown, so the shared
 *    ownership is safe for a daemon lifetime.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <dirent.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>

#define SPOOLC "/usr/lib/gridengine/libspoolc.so"
#define SGE_TYPE_ALL 31

static const char *inits[] = {
    "bootstrap_mt_init", "feature_mt_init", "obj_mt_init",
    "prof_mt_init", "sc_mt_init", "uidgid_mt_init", 0,
};

__attribute__((constructor)) static void spoolc_mt_fix(void) {
    void *h = dlopen(SPOOLC, RTLD_LAZY | RTLD_NODELETE);
    if (h) {
        for (const char **n = inits; *n; n++) {
            void (*init)(void) = (void (*)(void))dlsym(h, *n);
            if (init) init();
        }
    }
}

static void sync_spoolc_state(void) {
    static int ctx_synced = 0;
    void *h = dlopen(SPOOLC, RTLD_LAZY | RTLD_NOLOAD);
    if (!h) return;

    if (!ctx_synced) {
        void *(*exe_get)(void) =
            (void *(*)(void))dlsym(RTLD_DEFAULT, "spool_get_default_context");
        void *(*lib_get)(void) = (void *(*)(void))dlsym(h, "spool_get_default_context");
        void (*lib_set)(void *) = (void (*)(void *))dlsym(h, "spool_set_default_context");
        if (exe_get && lib_get && lib_set && exe_get != lib_get) {
            void *ctx = exe_get();
            if (ctx && lib_get() == NULL) {
                lib_set(ctx);
                ctx_synced = 1;
            }
        }
    }

    /* Master-list aliasing is only correct (and only needed) inside
     * sge_qmaster: other daemons opendir before their own object state
     * exists, and calling the exe-side getter there recreates the
     * uninitialized-key crash this shim exists to fix. */
    if (strcmp(program_invocation_short_name, "sge_qmaster") == 0) {
        void **(*exe_ml)(int) = (void **(*)(int))dlsym(RTLD_DEFAULT, "object_type_get_master_list");
        void **(*lib_ml)(int) = (void **(*)(int))dlsym(h, "object_type_get_master_list");
        if (exe_ml && lib_ml && exe_ml != lib_ml) {
            static char dead[SGE_TYPE_ALL];
            int t;
            for (t = 0; t < SGE_TYPE_ALL; t++) {
                void **exe_slot, **lib_slot;
                if (dead[t]) continue;
                exe_slot = exe_ml(t);
                lib_slot = lib_ml(t);
                if (!exe_slot || !lib_slot) {
                    /* a NULL slot never becomes non-NULL in global mode;
                     * remember it so the getter's ERROR log fires once,
                     * not once per opendir */
                    dead[t] = 1;
                    continue;
                }
                if (lib_slot != exe_slot && *exe_slot && *lib_slot != *exe_slot) {
                    *lib_slot = *exe_slot;
                }
            }
        }
    }
}

DIR *opendir(const char *name) {
    static DIR *(*real)(const char *) = 0;
    if (!real) real = (DIR *(*)(const char *))dlsym(RTLD_NEXT, "opendir");
    sync_spoolc_state();
    return real(name);
}
