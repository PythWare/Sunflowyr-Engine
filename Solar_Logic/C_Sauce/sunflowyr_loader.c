#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#define LOADER_VERSION "SunflowyrLoader 5"
#define PATH_CHARS 1024
#define NAME_BYTES 512
#define MAX_HANDLES 1024
#define MAX_DEPTH 6
#define MAX_HEAD_BYTES (256u * 1024u * 1024u)
#define RESOURCE_ALIGNMENT 32ull
#define PRELOAD_BYTES (256u * 1024u)

enum { ADD_NONE, ADD_RAW, ADD_CLASS, ADD_SCRIPT };
enum { PATCH_EXTERNAL = 1, PATCH_RESOURCE = 2 };

typedef struct {
    unsigned int offset;
    int width;
    int kind;
    char target[NAME_BYTES];
    unsigned long long value;
    int host_container;
    int host_segment;
} Patch;

typedef struct {
    unsigned int count_offset;
    unsigned int insert_offset;
    unsigned int base_size;
    int count_add;
    int sequence;
    int mod_index;
    int number;
    wchar_t blob[PATH_CHARS];
    Patch *fixes;
    int fix_count;
    int fix_capacity;
} Append;

typedef struct {
    long long path_id;
    unsigned long long offset;
    unsigned int size;
    Append *appends;
    int append_count;
    int append_capacity;
    unsigned char *memory;
    int found;
    int add_kind;
    int type_id;
    unsigned short class_id;
    short script_index;
    unsigned char stripped;
    unsigned char type_hash[16];
    unsigned char script_id[16];
    long long script_path_id;
    char script_file[NAME_BYTES];
    int owner;
    Patch *patches;
    int patch_count;
    int patch_capacity;
    HANDLE file;
    wchar_t source[PATH_CHARS];
} Segment;

typedef struct {
    wchar_t name[PATH_CHARS];
    wchar_t full[PATH_CHARS];
    int state;
    int raw;
    unsigned long long real_size;
    unsigned long long real_data_offset;
    unsigned long long real_end;
    unsigned long long virtual_size;
    unsigned int head_size;
    unsigned char *head;
    Segment *segments;
    int segment_count;
    int segment_capacity;
    int *order;
    int order_count;
    CRITICAL_SECTION lock;
} Container;

typedef struct {
    HANDLE handle;
    Container *container;
    unsigned long long position;
} Tracked;

typedef struct {
    unsigned char *data;
    size_t size;
    size_t capacity;
} Buffer;

typedef HANDLE (WINAPI *CreateFileWFn)(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
typedef BOOL (WINAPI *ReadFileFn)(HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED);
typedef BOOL (WINAPI *SetFilePointerExFn)(HANDLE, LARGE_INTEGER, PLARGE_INTEGER, DWORD);
typedef DWORD (WINAPI *SetFilePointerFn)(HANDLE, LONG, PLONG, DWORD);
typedef DWORD (WINAPI *GetFileSizeFn)(HANDLE, LPDWORD);
typedef BOOL (WINAPI *GetFileInformationByHandleFn)(HANDLE, LPBY_HANDLE_FILE_INFORMATION);
typedef BOOL (WINAPI *GetFileAttributesExWFn)(LPCWSTR, GET_FILEEX_INFO_LEVELS, LPVOID);
typedef HANDLE (WINAPI *FindFirstFileWFn)(LPCWSTR, LPWIN32_FIND_DATAW);
typedef BOOL (WINAPI *CloseHandleFn)(HANDLE);

static CreateFileWFn real_CreateFileW;
static ReadFileFn real_ReadFile;
static SetFilePointerExFn real_SetFilePointerEx;
static SetFilePointerFn real_SetFilePointer;
static GetFileSizeFn real_GetFileSize;
static GetFileInformationByHandleFn real_GetFileInformationByHandle;
static GetFileAttributesExWFn real_GetFileAttributesExW;
static FindFirstFileWFn real_FindFirstFileW;
static CloseHandleFn real_CloseHandle;

static wchar_t data_dir[PATH_CHARS];
static wchar_t mods_dir[PATH_CHARS];
static Container *containers;
static int container_count;
static int container_capacity;
static int mod_counter;
static long long host_counter;
static Tracked tracked[MAX_HANDLES];
static CRITICAL_SECTION table_lock;
static CRITICAL_SECTION log_lock;
static INIT_ONCE discovery_once = INIT_ONCE_STATIC_INIT;
static FILE *log_file;
static int hooks_installed;

__declspec(dllexport) const char *SunflowyrLoaderVersion(void) { return LOADER_VERSION; }

static int index_segments(Container *container);

static void log_line(const char *format, ...) __attribute__((format(printf, 1, 2)));

static void log_line(const char *format, ...) {
    if (!log_file) return;
    va_list args;
    va_start(args, format);
    EnterCriticalSection(&log_lock);
    SYSTEMTIME now;
    GetLocalTime(&now);
    fprintf(log_file, "%02d:%02d:%02d.%03d ", now.wHour, now.wMinute, now.wSecond, now.wMilliseconds);
    vfprintf(log_file, format, args);
    fputc('\n', log_file);
    fflush(log_file);
    LeaveCriticalSection(&log_lock);
    va_end(args);
}

static const char *narrow(const wchar_t *text, char *buffer, int size) {
    if (!WideCharToMultiByte(CP_UTF8, 0, text, -1, buffer, size, NULL, NULL)) buffer[0] = 0;
    return buffer;
}

static void lower_in_place(wchar_t *text) {
    for (; *text; text++) {
        if (*text == L'/') *text = L'\\';
        *text = (wchar_t)towlower(*text);
    }
}

static int normalize_path(const wchar_t *input, wchar_t *output) {
    const wchar_t *source = input;
    if (wcsncmp(source, L"\\\\?\\", 4) == 0) source += 4;
    wchar_t slashed[PATH_CHARS];
    size_t length = wcslen(source);
    if (length >= PATH_CHARS) return 0;
    for (size_t index = 0; index <= length; index++) slashed[index] = source[index] == L'/' ? L'\\' : source[index];
    DWORD written = GetFullPathNameW(slashed, PATH_CHARS, output, NULL);
    if (written == 0 || written >= PATH_CHARS) return 0;
    lower_in_place(output);
    return 1;
}

static int find_container_index(const wchar_t *name) {
    for (int index = 0; index < container_count; index++) {
        if (wcscmp(containers[index].name, name) == 0) return index;
    }
    return -1;
}

static int add_container(const wchar_t *name) {
    int existing = find_container_index(name);
    if (existing >= 0) return existing;
    if (container_count == container_capacity) {
        int capacity = container_capacity ? container_capacity * 2 : 16;
        Container *grown = realloc(containers, sizeof(Container) * capacity);
        if (!grown) return -1;
        containers = grown;
        container_capacity = capacity;
    }
    Container *container = &containers[container_count];
    memset(container, 0, sizeof(*container));
    wcsncpy(container->name, name, PATH_CHARS - 1);
    wchar_t joined[PATH_CHARS];
    if (_snwprintf(joined, PATH_CHARS, L"%ls\\%ls", data_dir, name) < 0) return -1;
    if (!normalize_path(joined, container->full)) return -1;
    return container_count++;
}

static int segment_for(int container_index, long long path_id) {
    Container *container = &containers[container_index];
    for (int index = 0; index < container->segment_count; index++) {
        if (container->segments[index].path_id == path_id) return index;
    }
    if (container->segment_count == container->segment_capacity) {
        int capacity = container->segment_capacity ? container->segment_capacity * 2 : 16;
        Segment *grown = realloc(container->segments, sizeof(Segment) * capacity);
        if (!grown) return -1;
        container->segments = grown;
        container->segment_capacity = capacity;
    }
    Segment *segment = &container->segments[container->segment_count];
    memset(segment, 0, sizeof(*segment));
    segment->path_id = path_id;
    segment->file = INVALID_HANDLE_VALUE;
    segment->owner = -1;
    return container->segment_count++;
}

static int overrides;

static void add_override(const wchar_t *container_name, long long path_id, const wchar_t *source, const char *mod_name) {
    int container_index = add_container(container_name);
    if (container_index < 0) return;
    int segment_index = segment_for(container_index, path_id);
    if (segment_index < 0) return;
    Segment *segment = &containers[container_index].segments[segment_index];
    (void)mod_name;
    if (segment->source[0]) overrides++;
    wcsncpy(segment->source, source, PATH_CHARS - 1);
}

static int parse_hash(const char *text, unsigned char *out) {
    if (strlen(text) != 32) return 0;
    for (int index = 0; index < 16; index++) {
        unsigned int value;
        if (sscanf(text + index * 2, "%2x", &value) != 1) return 0;
        out[index] = (unsigned char)value;
    }
    return 1;
}

static void copy_rest(char *target, const char *rest) {
    while (*rest == ' ' || *rest == '\t') rest++;
    strncpy(target, rest, NAME_BYTES - 1);
    target[NAME_BYTES - 1] = 0;
    size_t length = strlen(target);
    while (length && (target[length - 1] == '\r' || target[length - 1] == '\n' || target[length - 1] == ' ')) target[--length] = 0;
}

static Segment *descriptor_segment(int container_index, long long path_id, int mod_index) {
    int segment_index = segment_for(container_index, path_id);
    if (segment_index < 0) return NULL;
    Segment *segment = &containers[container_index].segments[segment_index];
    if (segment->owner != mod_index) {
        segment->owner = mod_index;
        segment->add_kind = ADD_NONE;
        segment->patch_count = 0;
    }
    return segment;
}

static void add_patch(Segment *segment, unsigned int offset, int width, int kind, const char *rest) {
    if (segment->patch_count == segment->patch_capacity) {
        int capacity = segment->patch_capacity ? segment->patch_capacity * 2 : 4;
        Patch *grown = realloc(segment->patches, sizeof(Patch) * capacity);
        if (!grown) return;
        segment->patches = grown;
        segment->patch_capacity = capacity;
    }
    Patch *patch = &segment->patches[segment->patch_count++];
    memset(patch, 0, sizeof(*patch));
    patch->offset = offset;
    patch->width = width;
    patch->kind = kind;
    patch->host_container = -1;
    patch->host_segment = -1;
    copy_rest(patch->target, rest);
}

static int append_sequence;

static Append *find_append(Segment *segment, int mod_index, int number) {
    for (int index = 0; index < segment->append_count; index++) {
        if (segment->appends[index].mod_index == mod_index && segment->appends[index].number == number) return &segment->appends[index];
    }
    return NULL;
}

static void add_append(int container_index, long long path_id, int mod_index, int number, unsigned int count_offset, unsigned int insert_offset, int count_add, unsigned int base_size, const wchar_t *folder, const char *rest) {
    int segment_index = segment_for(container_index, path_id);
    if (segment_index < 0) return;
    Segment *segment = &containers[container_index].segments[segment_index];
    if (segment->append_count == segment->append_capacity) {
        int capacity = segment->append_capacity ? segment->append_capacity * 2 : 4;
        Append *grown = realloc(segment->appends, sizeof(Append) * capacity);
        if (!grown) return;
        segment->appends = grown;
        segment->append_capacity = capacity;
    }
    Append *append = &segment->appends[segment->append_count++];
    memset(append, 0, sizeof(*append));
    append->count_offset = count_offset;
    append->insert_offset = insert_offset;
    append->count_add = count_add;
    append->base_size = base_size;
    append->sequence = append_sequence++;
    append->mod_index = mod_index;
    append->number = number;
    char name[NAME_BYTES];
    copy_rest(name, rest);
    wchar_t wide[PATH_CHARS];
    if (!MultiByteToWideChar(CP_UTF8, 0, name, -1, wide, PATH_CHARS)) wide[0] = 0;
    _snwprintf(append->blob, PATH_CHARS, L"%ls\\%ls", folder, wide);
}

static void add_append_fix(int container_index, long long path_id, int mod_index, int number, unsigned int offset, const char *rest) {
    int segment_index = segment_for(container_index, path_id);
    if (segment_index < 0) return;
    Append *append = find_append(&containers[container_index].segments[segment_index], mod_index, number);
    if (!append) return;
    if (append->fix_count == append->fix_capacity) {
        int capacity = append->fix_capacity ? append->fix_capacity * 2 : 4;
        Patch *grown = realloc(append->fixes, sizeof(Patch) * capacity);
        if (!grown) return;
        append->fixes = grown;
        append->fix_capacity = capacity;
    }
    Patch *patch = &append->fixes[append->fix_count++];
    memset(patch, 0, sizeof(*patch));
    patch->offset = offset;
    patch->width = 4;
    patch->kind = PATCH_EXTERNAL;
    copy_rest(patch->target, rest);
}

static void read_descriptor(const wchar_t *list_path, const wchar_t *container_name, int mod_index) {
    FILE *list = _wfopen(list_path, L"rb");
    if (!list) return;
    int container_index = add_container(container_name);
    wchar_t folder[PATH_CHARS];
    wcsncpy(folder, list_path, PATH_CHARS - 1);
    folder[PATH_CHARS - 1] = 0;
    wchar_t *slash = wcsrchr(folder, L'\\');
    if (slash) *slash = 0;
    char line[2048];
    while (container_index >= 0 && fgets(line, sizeof(line), list)) {
        long long path_id, script_path_id;
        int type_id, class_id, script_index, stripped, consumed, number, count_add;
        unsigned int offset, insert_offset;
        char hash_a[64], hash_b[64];
        Segment *segment;
        if (line[0] == '#') continue;
        unsigned int base_size;
        if (sscanf(line, "append %lld %d %u %u %d %u %n", &path_id, &number, &offset, &insert_offset, &count_add, &base_size, &consumed) == 6) {
            add_append(container_index, path_id, mod_index, number, offset, insert_offset, count_add, base_size, folder, line + consumed);
        } else if (sscanf(line, "appendfix %lld %d %u %n", &path_id, &number, &offset, &consumed) == 3) {
            add_append_fix(container_index, path_id, mod_index, number, offset, line + consumed);
        } else if (sscanf(line, "new %lld %d %d %63s", &path_id, &class_id, &stripped, hash_a) == 4) {
            if (!(segment = descriptor_segment(container_index, path_id, mod_index)) || !parse_hash(hash_a, segment->type_hash)) continue;
            segment->add_kind = ADD_CLASS;
            segment->class_id = (unsigned short)class_id;
            segment->stripped = (unsigned char)stripped;
        } else if (sscanf(line, "script %lld %d %lld %63s %63s %n", &path_id, &stripped, &script_path_id, hash_a, hash_b, &consumed) == 5) {
            if (!(segment = descriptor_segment(container_index, path_id, mod_index))) continue;
            if (!parse_hash(hash_a, segment->script_id) || !parse_hash(hash_b, segment->type_hash)) continue;
            segment->add_kind = ADD_SCRIPT;
            segment->class_id = 114;
            segment->stripped = (unsigned char)stripped;
            segment->script_path_id = script_path_id;
            copy_rest(segment->script_file, line + consumed);
        } else if (sscanf(line, "fix %lld %u %n", &path_id, &offset, &consumed) == 2) {
            if ((segment = descriptor_segment(container_index, path_id, mod_index))) add_patch(segment, offset, 4, PATCH_EXTERNAL, line + consumed);
        } else if (sscanf(line, "resource %lld %u %n", &path_id, &offset, &consumed) == 2) {
            if ((segment = descriptor_segment(container_index, path_id, mod_index))) add_patch(segment, offset, 8, PATCH_RESOURCE, line + consumed);
        } else if (sscanf(line, "%lld %d %d %d %d", &path_id, &type_id, &class_id, &script_index, &stripped) == 5) {
            if (!(segment = descriptor_segment(container_index, path_id, mod_index))) continue;
            segment->add_kind = ADD_RAW;
            segment->type_id = type_id;
            segment->class_id = (unsigned short)class_id;
            segment->script_index = (short)script_index;
            segment->stripped = (unsigned char)stripped;
        }
    }
    fclose(list);
}

static int parse_path_id(const wchar_t *file_name, long long *path_id) {
    const wchar_t *dot = wcsrchr(file_name, L'.');
    if (!dot || _wcsicmp(dot, L".bin") != 0 || dot == file_name) return 0;
    const wchar_t *digits = file_name;
    for (const wchar_t *marker = file_name; (marker = wcsstr(marker, L"__path")) != NULL && marker < dot; marker++) digits = marker + 6;
    const wchar_t *cursor = digits;
    if (*cursor == L'-') cursor++;
    if (cursor == dot) return 0;
    for (; cursor < dot; cursor++) if (*cursor < L'0' || *cursor > L'9') return 0;
    *path_id = _wcstoi64(digits, NULL, 10);
    return 1;
}

static void scan_folder(const wchar_t *folder, const wchar_t *relative, int depth, const char *mod_name, int mod_index) {
    if (depth > MAX_DEPTH) return;
    wchar_t pattern[PATH_CHARS];
    if (_snwprintf(pattern, PATH_CHARS, L"%ls\\*", folder) < 0) return;
    WIN32_FIND_DATAW found;
    HANDLE search = real_FindFirstFileW(pattern, &found);
    if (search == INVALID_HANDLE_VALUE) return;
    do {
        if (wcscmp(found.cFileName, L".") == 0 || wcscmp(found.cFileName, L"..") == 0) continue;
        wchar_t child[PATH_CHARS];
        if (_snwprintf(child, PATH_CHARS, L"%ls\\%ls", folder, found.cFileName) < 0) continue;
        if (found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            wchar_t child_relative[PATH_CHARS];
            if (relative[0]) {
                if (_snwprintf(child_relative, PATH_CHARS, L"%ls\\%ls", relative, found.cFileName) < 0) continue;
            } else {
                wcsncpy(child_relative, found.cFileName, PATH_CHARS - 1);
                child_relative[PATH_CHARS - 1] = 0;
            }
            scan_folder(child, child_relative, depth + 1, mod_name, mod_index);
            continue;
        }
        if (!relative[0]) continue;
        wchar_t container_name[PATH_CHARS];
        wcsncpy(container_name, relative, PATH_CHARS - 1);
        container_name[PATH_CHARS - 1] = 0;
        lower_in_place(container_name);
        long long path_id;
        if (_wcsicmp(found.cFileName, L"new_objects.txt") == 0) read_descriptor(child, container_name, mod_index);
        else if (parse_path_id(found.cFileName, &path_id)) add_override(container_name, path_id, child, mod_name);
    } while (FindNextFileW(search, &found));
    FindClose(search);
}

static void load_mod(const wchar_t *name) {
    wchar_t folder[PATH_CHARS];
    char text[PATH_CHARS * 3];
    if (_snwprintf(folder, PATH_CHARS, L"%ls\\%ls", mods_dir, name) < 0) return;
    DWORD attributes = GetFileAttributesW(folder);
    if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)) {
        log_line("mod %s is listed in mods.txt but its folder is missing", narrow(name, text, sizeof(text)));
        return;
    }
    narrow(name, text, sizeof(text));
    log_line("loading mod %s", text);
    overrides = 0;
    scan_folder(folder, L"", 0, text, mod_counter++);
    if (overrides) log_line("%s replaces %d objects that an earlier mod also changes, the later mod wins", text, overrides);
}

static int compare_names(const void *left, const void *right) {
    return _wcsicmp((const wchar_t *)left, (const wchar_t *)right);
}

static void trim_line(char *line) {
    size_t length = strlen(line);
    while (length && (line[length - 1] == '\n' || line[length - 1] == '\r' || line[length - 1] == ' ' || line[length - 1] == '\t')) line[--length] = 0;
}

static int read_mod_list(void) {
    wchar_t list_path[PATH_CHARS];
    if (_snwprintf(list_path, PATH_CHARS, L"%ls\\mods.txt", mods_dir) < 0) return 0;
    FILE *list = _wfopen(list_path, L"rb");
    if (!list) return 0;
    char line[PATH_CHARS * 3];
    int first = 1;
    while (fgets(line, sizeof(line), list)) {
        char *start = line;
        if (first && (unsigned char)start[0] == 0xEF && (unsigned char)start[1] == 0xBB && (unsigned char)start[2] == 0xBF) start += 3;
        first = 0;
        trim_line(start);
        while (*start == ' ' || *start == '\t') start++;
        if (!*start || *start == '#') continue;
        wchar_t name[PATH_CHARS];
        if (!MultiByteToWideChar(CP_UTF8, 0, start, -1, name, PATH_CHARS)) continue;
        load_mod(name);
    }
    fclose(list);
    return 1;
}

static void read_mod_folders(void) {
    wchar_t pattern[PATH_CHARS];
    if (_snwprintf(pattern, PATH_CHARS, L"%ls\\*", mods_dir) < 0) return;
    WIN32_FIND_DATAW found;
    HANDLE search = real_FindFirstFileW(pattern, &found);
    if (search == INVALID_HANDLE_VALUE) return;
    int count = 0, capacity = 16;
    wchar_t (*names)[MAX_PATH] = malloc(sizeof(*names) * capacity);
    do {
        if (!(found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        if (found.cFileName[0] == L'.') continue;
        if (count == capacity) {
            capacity *= 2;
            void *grown = realloc(names, sizeof(*names) * capacity);
            if (!grown) break;
            names = grown;
        }
        wcsncpy(names[count++], found.cFileName, MAX_PATH - 1);
        names[count - 1][MAX_PATH - 1] = 0;
    } while (FindNextFileW(search, &found));
    FindClose(search);
    qsort(names, count, sizeof(*names), compare_names);
    for (int index = 0; index < count; index++) load_mod(names[index]);
    free(names);
}

static int segment_order(const void *left, const void *right) {
    long long a = ((const Segment *)left)->path_id, b = ((const Segment *)right)->path_id;
    return a < b ? -1 : a > b;
}

static int container_order(const void *left, const void *right) {
    return wcscmp(containers[*(const int *)left].name, containers[*(const int *)right].name);
}

static int file_size(const wchar_t *path, unsigned long long *size) {
    WIN32_FILE_ATTRIBUTE_DATA info;
    if (!real_GetFileAttributesExW(path, GetFileExInfoStandard, &info)) return 0;
    *size = ((unsigned long long)info.nFileSizeHigh << 32) | info.nFileSizeLow;
    return 1;
}

static void attach_resources(void) {
    char text[PATH_CHARS * 3];
    int original = container_count;
    int *order = malloc(sizeof(int) * (original ? original : 1));
    if (!order) return;
    for (int index = 0; index < original; index++) {
        order[index] = index;
        qsort(containers[index].segments, containers[index].segment_count, sizeof(Segment), segment_order);
    }
    qsort(order, original, sizeof(int), container_order);
    for (int position = 0; position < original; position++) {
        int container_index = order[position];
        for (int segment_index = 0; segment_index < containers[container_index].segment_count; segment_index++) {
            for (int patch_index = 0; patch_index < containers[container_index].segments[segment_index].patch_count; patch_index++) {
                Segment *segment = &containers[container_index].segments[segment_index];
                Patch *patch = &segment->patches[patch_index];
                if (patch->kind != PATCH_RESOURCE) continue;
                if (!segment->source[0]) {
                    log_line("%s path %lld: resource listed without a .bin file", narrow(containers[container_index].name, text, sizeof(text)), segment->path_id);
                    continue;
                }
                wchar_t sidecar[PATH_CHARS];
                wcsncpy(sidecar, segment->source, PATH_CHARS - 1);
                sidecar[PATH_CHARS - 1] = 0;
                wchar_t *dot = wcsrchr(sidecar, L'.');
                if (!dot || (size_t)(dot - sidecar) + 10 >= PATH_CHARS) continue;
                wcscpy(dot, L".resource");
                wchar_t host_name[PATH_CHARS];
                if (!MultiByteToWideChar(CP_UTF8, 0, patch->target, -1, host_name, PATH_CHARS)) continue;
                lower_in_place(host_name);
                int host = add_container(host_name);
                if (host < 0) continue;
                containers[host].raw = 1;
                int host_segment = segment_for(host, --host_counter);
                if (host_segment < 0) continue;
                wcsncpy(containers[host].segments[host_segment].source, sidecar, PATH_CHARS - 1);
                Segment *refreshed = &containers[container_index].segments[segment_index];
                refreshed->patches[patch_index].host_container = host;
                refreshed->patches[patch_index].host_segment = host_segment;
            }
        }
    }
    free(order);
    for (int host = 0; host < container_count; host++) {
        Container *container = &containers[host];
        if (!container->raw) continue;
        narrow(container->name, text, sizeof(text));
        if (!file_size(container->full, &container->real_size)) {
            log_line("%s: the resource file couldnt be found, its sounds stay silent", text);
            container->state = -1;
            continue;
        }
        unsigned long long append = container->real_size;
        for (int index = 0; index < container->segment_count; index++) {
            Segment *segment = &container->segments[index];
            unsigned long long size;
            char source[PATH_CHARS * 3];
            if (!file_size(segment->source, &size) || size > 0xFFFFFFFFull) {
                log_line("%s: %s couldnt be read", text, narrow(segment->source, source, sizeof(source)));
                continue;
            }
            append = (append + RESOURCE_ALIGNMENT - 1) & ~(RESOURCE_ALIGNMENT - 1);
            segment->offset = append;
            segment->size = (unsigned int)size;
            segment->found = 1;
            append += size;
        }
        container->head_size = 0;
        container->real_data_offset = 0;
        container->real_end = container->real_size;
        container->virtual_size = append;
        container->state = index_segments(container) ? 1 : -1;
        if (container->state != 1) {
            log_line("%s: out of memory while indexing the resource blocks, its sounds stay silent", text);
            continue;
        }
        log_line("%s: %d sound blocks added", text, container->segment_count);
    }
    for (int index = 0; index < original; index++) {
        for (int segment_index = 0; segment_index < containers[index].segment_count; segment_index++) {
            Segment *segment = &containers[index].segments[segment_index];
            for (int patch_index = 0; patch_index < segment->patch_count; patch_index++) {
                Patch *patch = &segment->patches[patch_index];
                if (patch->kind != PATCH_RESOURCE || patch->host_container < 0) continue;
                patch->value = containers[patch->host_container].segments[patch->host_segment].offset;
            }
        }
    }
}

static BOOL CALLBACK discover(PINIT_ONCE once, PVOID parameter, PVOID *context) {
    (void)once; (void)parameter; (void)context;
    char text[PATH_CHARS * 3];
    log_line("%s, data folder %s", LOADER_VERSION, narrow(data_dir, text, sizeof(text)));
    if (!read_mod_list()) read_mod_folders();
    attach_resources();
    int total = 0;
    for (int index = 0; index < container_count; index++) {
        InitializeCriticalSection(&containers[index].lock);
        if (containers[index].raw) continue;
        total += containers[index].segment_count;
    }
    log_line("%d modded objects in %d game files", total, container_count);
    return TRUE;
}

static void ensure_discovered(void) {
    InitOnceExecuteOnce(&discovery_once, discover, NULL, NULL);
}

static unsigned int read_be32(const unsigned char *bytes) {
    return ((unsigned int)bytes[0] << 24) | ((unsigned int)bytes[1] << 16) | ((unsigned int)bytes[2] << 8) | bytes[3];
}

static void write_be32(unsigned char *bytes, unsigned int value) {
    bytes[0] = (unsigned char)(value >> 24);
    bytes[1] = (unsigned char)(value >> 16);
    bytes[2] = (unsigned char)(value >> 8);
    bytes[3] = (unsigned char)value;
}

static int read_at(HANDLE file, unsigned long long offset, void *buffer, DWORD size, DWORD *got) {
    OVERLAPPED overlapped;
    memset(&overlapped, 0, sizeof(overlapped));
    overlapped.Offset = (DWORD)offset;
    overlapped.OffsetHigh = (DWORD)(offset >> 32);
    overlapped.hEvent = CreateEventW(NULL, TRUE, FALSE, NULL);
    *got = 0;
    BOOL ok = real_ReadFile(file, buffer, size, got, &overlapped);
    if (!ok && GetLastError() == ERROR_IO_PENDING) ok = GetOverlappedResult(file, &overlapped, got, TRUE);
    if (overlapped.hEvent) real_CloseHandle(overlapped.hEvent);
    return ok || GetLastError() == ERROR_HANDLE_EOF;
}

static int read_fully(HANDLE file, unsigned long long offset, unsigned char *buffer, unsigned long long size) {
    while (size) {
        DWORD chunk = size > 0x1000000 ? 0x1000000 : (DWORD)size;
        DWORD got;
        if (!read_at(file, offset, buffer, chunk, &got) || got == 0) return 0;
        offset += got;
        buffer += got;
        size -= got;
    }
    return 1;
}

static int buffer_put(Buffer *buffer, const void *data, size_t size) {
    if (buffer->size + size > buffer->capacity) {
        size_t capacity = buffer->capacity ? buffer->capacity : 4096;
        while (capacity < buffer->size + size) capacity *= 2;
        unsigned char *grown = realloc(buffer->data, capacity);
        if (!grown) return 0;
        buffer->data = grown;
        buffer->capacity = capacity;
    }
    if (data) memcpy(buffer->data + buffer->size, data, size);
    else memset(buffer->data + buffer->size, 0, size);
    buffer->size += size;
    return 1;
}

static int buffer_align(Buffer *buffer) {
    size_t padding = (4 - ((20 + buffer->size) & 3)) & 3;
    return buffer_put(buffer, NULL, padding);
}

static int payload_size(Segment *segment, const char *text) {
    unsigned long long size;
    char source[PATH_CHARS * 3];
    if (!segment->source[0]) {
        log_line("%s path %lld: listed in new_objects.txt but its .bin file is missing", text, segment->path_id);
        return 0;
    }
    if (!file_size(segment->source, &size) || size > 0xFFFFFFFFull) {
        log_line("%s path %lld: %s couldnt be read", text, segment->path_id, narrow(segment->source, source, sizeof(source)));
        return 0;
    }
    segment->size = (unsigned int)size;
    return 1;
}

typedef struct {
    unsigned long long offset;
    int index;
} Placed;

static int placed_order(const void *left, const void *right) {
    const Placed *a = left, *b = right;
    return a->offset < b->offset ? -1 : a->offset > b->offset;
}

static int index_segments(Container *container) {
    Placed *placed = malloc(sizeof(Placed) * (container->segment_count ? container->segment_count : 1));
    if (!placed) return 0;
    int count = 0;
    for (int index = 0; index < container->segment_count; index++) {
        Segment *segment = &container->segments[index];
        if (segment->size && segment->offset >= container->real_end) {
            placed[count].offset = segment->offset;
            placed[count].index = index;
            count++;
        }
    }
    qsort(placed, count, sizeof(Placed), placed_order);
    free(container->order);
    container->order = malloc(sizeof(int) * (count ? count : 1));
    if (!container->order) {
        free(placed);
        container->order_count = 0;
        return 0;
    }
    for (int index = 0; index < count; index++) container->order[index] = placed[index].index;
    container->order_count = count;
    free(placed);
    return 1;
}

static int place_segment(Segment *segment, unsigned long long *append, unsigned long long data_start, const char *text) {
    unsigned long long relative = (*append - data_start + 7) & ~7ull;
    if (relative + segment->size > 0xFFFFFFFFull) {
        log_line("%s path %lld: the file would pass 4 GB, skipped", text, segment->path_id);
        segment->size = 0;
        return 0;
    }
    segment->offset = data_start + relative;
    *append = segment->offset + segment->size;
    return 1;
}

typedef struct {
    int class_id;
    int scripted;
    unsigned char script_id[16];
    unsigned char hash[16];
} NewType;

typedef struct {
    int file_index;
    long long path_id;
} ScriptRef;

typedef struct {
    unsigned char *head;
    unsigned int type_count;
    unsigned int *type_start;
    int *type_class;
    NewType *new_types;
    int new_type_count;
    int script_count;
    ScriptRef *scripts;
    int new_script_count;
    int external_count;
    const char **external_paths;
    char (*new_externals)[NAME_BYTES];
    int new_external_count;
} Tables;

static int resolve_external(Tables *tables, const char *name) {
    if (!name[0] || strcmp(name, ".") == 0) return 0;
    for (int index = 0; index < tables->external_count; index++) {
        if (_stricmp(tables->external_paths[index], name) == 0) return index + 1;
    }
    for (int index = 0; index < tables->new_external_count; index++) {
        if (_stricmp(tables->new_externals[index], name) == 0) return tables->external_count + index + 1;
    }
    void *grown = realloc(tables->new_externals, sizeof(*tables->new_externals) * (tables->new_external_count + 1));
    if (!grown) return -1;
    tables->new_externals = grown;
    strncpy(tables->new_externals[tables->new_external_count], name, NAME_BYTES - 1);
    tables->new_externals[tables->new_external_count][NAME_BYTES - 1] = 0;
    tables->new_external_count++;
    return tables->external_count + tables->new_external_count;
}

static int resolve_script(Tables *tables, int file_index, long long path_id) {
    int total = tables->script_count + tables->new_script_count;
    for (int index = 0; index < total; index++) {
        if (tables->scripts[index].file_index == file_index && tables->scripts[index].path_id == path_id) return index;
    }
    void *grown = realloc(tables->scripts, sizeof(ScriptRef) * (total + 1));
    if (!grown) return -1;
    tables->scripts = grown;
    tables->scripts[total].file_index = file_index;
    tables->scripts[total].path_id = path_id;
    tables->new_script_count++;
    return total;
}

static int append_type(Tables *tables, int class_id, int scripted, const unsigned char *script_id, const unsigned char *hash) {
    void *grown = realloc(tables->new_types, sizeof(NewType) * (tables->new_type_count + 1));
    if (!grown) return 0;
    tables->new_types = grown;
    NewType *type = &tables->new_types[tables->new_type_count++];
    type->class_id = class_id;
    type->scripted = scripted;
    if (scripted) memcpy(type->script_id, script_id, 16);
    memcpy(type->hash, hash, 16);
    return 1;
}

static int resolve_class_type(Tables *tables, int class_id, const unsigned char *hash) {
    for (unsigned int index = 0; index < tables->type_count; index++) if (tables->type_class[index] == class_id) return class_id;
    for (int index = 0; index < tables->new_type_count; index++) if (tables->new_types[index].class_id == class_id) return class_id;
    return append_type(tables, class_id, 0, NULL, hash) ? class_id : 0x7FFFFFFF;
}

static int resolve_script_type(Tables *tables, const unsigned char *script_id, const unsigned char *hash) {
    int lowest = 0;
    for (unsigned int index = 0; index < tables->type_count; index++) {
        int class_id = tables->type_class[index];
        if (class_id >= 0) continue;
        if (class_id < lowest) lowest = class_id;
        if (memcmp(tables->head + tables->type_start[index] + 4, script_id, 16) == 0) return class_id;
    }
    for (int index = 0; index < tables->new_type_count; index++) {
        NewType *type = &tables->new_types[index];
        if (type->class_id < lowest) lowest = type->class_id;
        if (type->scripted && memcmp(type->script_id, script_id, 16) == 0) return type->class_id;
    }
    return append_type(tables, lowest - 1, 1, script_id, hash) ? lowest - 1 : 0x7FFFFFFF;
}

static void free_tables(Tables *tables) {
    free(tables->type_start);
    free(tables->type_class);
    free(tables->new_types);
    free(tables->scripts);
    free(tables->external_paths);
    free(tables->new_externals);
}

static int append_order(const void *left, const void *right) {
    const Append *a = *(const Append *const *)left, *b = *(const Append *const *)right;
    if (a->insert_offset != b->insert_offset) return a->insert_offset < b->insert_offset ? -1 : 1;
    return a->sequence < b->sequence ? -1 : a->sequence > b->sequence;
}

static int compose_appends(HANDLE file, const unsigned char *head, unsigned int data_offset, unsigned int entry, Segment *segment, Tables *tables, const char *text) {
    unsigned int start, size;
    memcpy(&start, head + entry + 8, 4);
    memcpy(&size, head + entry + 12, 4);
    unsigned char *original = malloc(size ? size : 1);
    unsigned char **blobs = calloc(segment->append_count, sizeof(unsigned char *));
    unsigned int *blob_sizes = calloc(segment->append_count, sizeof(unsigned int));
    Append **order = calloc(segment->append_count, sizeof(Append *));
    unsigned char *out = NULL;
    int result = 0;
    if (!original || !blobs || !blob_sizes || !order || !read_fully(file, (unsigned long long)data_offset + start, original, size)) goto finish;
    unsigned long long total = size;
    for (int index = 0; index < segment->append_count; index++) {
        Append *append = &segment->appends[index];
        order[index] = append;
        unsigned long long blob_size;
        char source[PATH_CHARS * 3];
        if (append->base_size != size) {
            log_line("%s path %lld: list addition %s was built for a %u byte object but the game has %u bytes, skipped (rebuild the mod)", text, segment->path_id, narrow(append->blob, source, sizeof(source)), append->base_size, size);
            append->count_add = 0;
            continue;
        }
        if (append->insert_offset > size || append->count_offset + 4 > size || !file_size(append->blob, &blob_size) || blob_size > 0x10000000ull) {
            log_line("%s path %lld: list addition %s doesnt fit this object, skipped", text, segment->path_id, narrow(append->blob, source, sizeof(source)));
            append->count_add = 0;
            continue;
        }
        HANDLE blob_file = real_CreateFileW(append->blob, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
        blobs[index] = malloc(blob_size ? (size_t)blob_size : 1);
        if (blob_file == INVALID_HANDLE_VALUE || !blobs[index] || (blob_size && !read_fully(blob_file, 0, blobs[index], blob_size))) {
            if (blob_file != INVALID_HANDLE_VALUE) real_CloseHandle(blob_file);
            log_line("%s path %lld: list addition %s couldnt be read", text, segment->path_id, narrow(append->blob, source, sizeof(source)));
            free(blobs[index]);
            blobs[index] = NULL;
            append->count_add = 0;
            continue;
        }
        real_CloseHandle(blob_file);
        blob_sizes[index] = (unsigned int)blob_size;
        for (int fix = 0; fix < append->fix_count; fix++) {
            Patch *patch = &append->fixes[fix];
            int value = resolve_external(tables, patch->target);
            if (patch->offset + 4 <= blob_sizes[index]) memcpy(blobs[index] + patch->offset, &value, 4);
        }
        total += blob_size;
    }
    if (total > 0xFFFFFFFFull) goto finish;
    out = malloc((size_t)total ? (size_t)total : 1);
    if (!out) goto finish;
    int *slot = calloc(segment->append_count, sizeof(int));
    if (!slot) goto finish;
    for (int index = 0; index < segment->append_count; index++) slot[index] = index;
    for (int left = 1; left < segment->append_count; left++) {
        int value = slot[left], right = left - 1;
        while (right >= 0 && append_order(&order[slot[right]], &order[value]) > 0) {
            slot[right + 1] = slot[right];
            right--;
        }
        slot[right + 1] = value;
    }
    unsigned int read_at_offset = 0, written = 0;
    for (int position = 0; position < segment->append_count; position++) {
        int index = slot[position];
        if (!blobs[index]) continue;
        Append *append = order[index];
        memcpy(out + written, original + read_at_offset, append->insert_offset - read_at_offset);
        written += append->insert_offset - read_at_offset;
        read_at_offset = append->insert_offset;
        memcpy(out + written, blobs[index], blob_sizes[index]);
        written += blob_sizes[index];
    }
    memcpy(out + written, original + read_at_offset, size - read_at_offset);
    written += size - read_at_offset;
    int added = 0;
    for (int index = 0; index < segment->append_count; index++) {
        Append *append = order[index];
        if (!blobs[index]) continue;
        unsigned int shift = 0;
        for (int other = 0; other < segment->append_count; other++) {
            if (blobs[other] && order[other]->insert_offset <= append->count_offset) shift += blob_sizes[other];
        }
        int count;
        memcpy(&count, out + append->count_offset + shift, 4);
        count += append->count_add;
        memcpy(out + append->count_offset + shift, &count, 4);
        added += append->count_add;
    }
    free(slot);
    segment->memory = out;
    segment->size = written;
    out = NULL;
    log_line("%s path %lld: %d list entries added", text, segment->path_id, added);
    result = 1;
finish:
    free(out);
    if (blobs) for (int index = 0; index < segment->append_count; index++) free(blobs[index]);
    free(blobs);
    free(blob_sizes);
    free(order);
    free(original);
    return result;
}

static int build_container(Container *container) {
    char text[PATH_CHARS * 3];
    narrow(container->name, text, sizeof(text));
    HANDLE file = real_CreateFileW(container->full, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        log_line("%s: the game file couldnt be opened (error %lu)", text, GetLastError());
        return 0;
    }
    int result = 0;
    unsigned char *head = NULL;
    unsigned int *entry_at = NULL;
    unsigned int *entry_out = NULL;
    Buffer out = {0};
    Tables tables;
    memset(&tables, 0, sizeof(tables));
    unsigned char header[20];
    LARGE_INTEGER size;
    if (!GetFileSizeEx(file, &size) || !read_fully(file, 0, header, sizeof(header))) {
        log_line("%s: the header couldnt be read", text);
        goto done;
    }
    unsigned int metadata_size = read_be32(header), stored_size = read_be32(header + 4);
    unsigned int format = read_be32(header + 8), data_offset = read_be32(header + 12);
    if (format != 15 || header[16] != 0) {
        log_line("%s: serialized format %u isnt supported, only 15 little endian", text, format);
        goto done;
    }
    if ((unsigned long long)stored_size != (unsigned long long)size.QuadPart || data_offset > stored_size || data_offset > MAX_HEAD_BYTES || 20ull + metadata_size > data_offset) {
        log_line("%s: the header doesnt match the file (size %u on disk %lld, data at %u)", text, stored_size, size.QuadPart, data_offset);
        goto done;
    }
    head = malloc(data_offset);
    if (!head || !read_fully(file, 0, head, data_offset)) {
        log_line("%s: the metadata couldnt be read", text);
        goto done;
    }
    tables.head = head;
    unsigned int end = 20 + metadata_size, cursor = 20;
    while (cursor < end && head[cursor]) cursor++;
    cursor += 1 + 4;
    if (cursor + 5 > end) goto malformed;
    int type_trees = head[cursor++];
    unsigned int type_count_offset = cursor;
    memcpy(&tables.type_count, head + cursor, 4);
    cursor += 4;
    if (tables.type_count > 100000) goto malformed;
    tables.type_start = malloc(sizeof(unsigned int) * (tables.type_count + 1));
    tables.type_class = malloc(sizeof(int) * (tables.type_count + 1));
    if (!tables.type_start || !tables.type_class) goto done;
    unsigned int types_begin = cursor;
    for (unsigned int index = 0; index < tables.type_count; index++) {
        if (cursor + 4 > end) goto malformed;
        int class_id;
        memcpy(&class_id, head + cursor, 4);
        tables.type_start[index] = cursor;
        tables.type_class[index] = class_id;
        cursor += 4 + (class_id < 0 ? 16 : 0) + 16;
        if (type_trees) {
            if (cursor + 8 > end) goto malformed;
            unsigned int node_count, string_size;
            memcpy(&node_count, head + cursor, 4);
            memcpy(&string_size, head + cursor + 4, 4);
            cursor += 8 + node_count * 24 + string_size;
        }
    }
    unsigned int types_end = cursor;
    if (cursor + 4 > end) goto malformed;
    int object_count;
    memcpy(&object_count, head + cursor, 4);
    cursor += 4;
    if (object_count < 0 || (unsigned int)object_count > metadata_size / 25) goto malformed;
    entry_at = malloc(sizeof(unsigned int) * (object_count + 1));
    entry_out = malloc(sizeof(unsigned int) * (object_count + container->segment_count + 1));
    if (!entry_at || !entry_out) goto done;
    qsort(container->segments, container->segment_count, sizeof(Segment), segment_order);
    for (int index = 0; index < object_count; index++) {
        cursor = (cursor + 3) & ~3u;
        if (cursor + 25 > end) goto malformed;
        entry_at[index] = cursor;
        long long path_id;
        memcpy(&path_id, head + cursor, 8);
        Segment key;
        key.path_id = path_id;
        Segment *segment = bsearch(&key, container->segments, container->segment_count, sizeof(Segment), segment_order);
        if (segment) segment->found = index + 1;
        cursor += 25;
    }
    if (cursor + 4 > end) goto malformed;
    memcpy(&tables.script_count, head + cursor, 4);
    cursor += 4;
    if (tables.script_count < 0 || tables.script_count > 1000000) goto malformed;
    tables.scripts = malloc(sizeof(ScriptRef) * (tables.script_count + 1));
    if (!tables.scripts) goto done;
    for (int index = 0; index < tables.script_count; index++) {
        if (cursor + 4 > end) goto malformed;
        memcpy(&tables.scripts[index].file_index, head + cursor, 4);
        cursor = ((cursor + 4) + 3) & ~3u;
        if (cursor + 8 > end) goto malformed;
        memcpy(&tables.scripts[index].path_id, head + cursor, 8);
        cursor += 8;
    }
    if (cursor + 4 > end) goto malformed;
    memcpy(&tables.external_count, head + cursor, 4);
    cursor += 4;
    if (tables.external_count < 0 || tables.external_count > 100000) goto malformed;
    tables.external_paths = malloc(sizeof(char *) * (tables.external_count + 1));
    if (!tables.external_paths) goto done;
    unsigned int externals_begin = cursor;
    for (int index = 0; index < tables.external_count; index++) {
        while (cursor < end && head[cursor]) cursor++;
        cursor += 1 + 16 + 4;
        if (cursor >= end) goto malformed;
        tables.external_paths[index] = (const char *)head + cursor;
        while (cursor < end && head[cursor]) cursor++;
        cursor += 1;
        if (cursor > end) goto malformed;
    }
    unsigned int externals_end = cursor;
    if (cursor > end) goto malformed;
    for (int index = 0; index < container->segment_count; index++) {
        Segment *segment = &container->segments[index];
        int in_memory = 0;
        if (segment->append_count && segment->found) {
            if (segment->source[0]) log_line("%s path %lld: a mod replaces this object whole, so list additions to it are skipped", text, segment->path_id);
            else if (compose_appends(file, head, data_offset, entry_at[segment->found - 1], segment, &tables, text)) in_memory = 1;
            else continue;
        }
        if (!in_memory && !segment->source[0] && !segment->found) {
            if (segment->add_kind) payload_size(segment, text);
            continue;
        }
        if (!segment->found && segment->add_kind == ADD_NONE) {
            log_line("%s path %lld: no such object in the game file and not listed in new_objects.txt", text, segment->path_id);
            continue;
        }
        if (!in_memory && !payload_size(segment, text)) continue;
        if (!segment->found) {
            if (segment->add_kind == ADD_CLASS) {
                segment->type_id = resolve_class_type(&tables, segment->class_id, segment->type_hash);
                segment->script_index = -1;
            } else if (segment->add_kind == ADD_SCRIPT) {
                int file_index = resolve_external(&tables, segment->script_file);
                segment->script_index = (short)resolve_script(&tables, file_index, segment->script_path_id);
                segment->type_id = resolve_script_type(&tables, segment->script_id, segment->type_hash);
            }
            if (segment->type_id == 0x7FFFFFFF) {
                segment->size = 0;
                continue;
            }
        }
        for (int patch_index = 0; patch_index < segment->patch_count; patch_index++) {
            Patch *patch = &segment->patches[patch_index];
            if (patch->kind == PATCH_EXTERNAL) patch->value = (unsigned long long)(unsigned int)resolve_external(&tables, patch->target);
            if (patch->offset + patch->width > segment->size) {
                log_line("%s path %lld: a patch at %u is past the end of the object", text, segment->path_id, patch->offset);
                patch->width = 0;
            }
        }
    }
    if (!buffer_put(&out, head + 20, type_count_offset - 20)) goto done;
    unsigned int total_types = tables.type_count + tables.new_type_count;
    if (!buffer_put(&out, &total_types, 4) || !buffer_put(&out, head + types_begin, types_end - types_begin)) goto done;
    for (int index = 0; index < tables.new_type_count; index++) {
        NewType *type = &tables.new_types[index];
        if (!buffer_put(&out, &type->class_id, 4)) goto done;
        if (type->scripted && !buffer_put(&out, type->script_id, 16)) goto done;
        if (!buffer_put(&out, type->hash, 16)) goto done;
        if (type_trees) {
            unsigned int empty[2] = {0, 0};
            if (!buffer_put(&out, empty, 8)) goto done;
        }
    }
    int placed_new = 0;
    for (int index = 0; index < container->segment_count; index++) {
        Segment *segment = &container->segments[index];
        if (!segment->found && segment->add_kind && segment->size) placed_new++;
    }
    int total_objects = object_count + placed_new;
    if (!buffer_put(&out, &total_objects, 4)) goto done;
    for (int index = 0; index < object_count; index++) {
        if (!buffer_align(&out)) goto done;
        entry_out[index] = (unsigned int)out.size;
        if (!buffer_put(&out, head + entry_at[index], 25)) goto done;
    }
    int new_index = object_count;
    for (int index = 0; index < container->segment_count; index++) {
        Segment *segment = &container->segments[index];
        if (segment->found || !segment->add_kind || !segment->size) continue;
        if (!buffer_align(&out)) goto done;
        entry_out[new_index++] = (unsigned int)out.size;
        unsigned char entry[25];
        memset(entry, 0, sizeof(entry));
        memcpy(entry, &segment->path_id, 8);
        memcpy(entry + 16, &segment->type_id, 4);
        memcpy(entry + 20, &segment->class_id, 2);
        memcpy(entry + 22, &segment->script_index, 2);
        entry[24] = segment->stripped;
        if (!buffer_put(&out, entry, 25)) goto done;
    }
    int total_scripts = tables.script_count + tables.new_script_count;
    if (!buffer_put(&out, &total_scripts, 4)) goto done;
    for (int index = 0; index < total_scripts; index++) {
        if (!buffer_put(&out, &tables.scripts[index].file_index, 4) || !buffer_align(&out) || !buffer_put(&out, &tables.scripts[index].path_id, 8)) goto done;
    }
    int total_externals = tables.external_count + tables.new_external_count;
    if (!buffer_put(&out, &total_externals, 4) || !buffer_put(&out, head + externals_begin, externals_end - externals_begin)) goto done;
    for (int index = 0; index < tables.new_external_count; index++) {
        unsigned char blank[21];
        memset(blank, 0, sizeof(blank));
        if (!buffer_put(&out, blank, 21) || !buffer_put(&out, tables.new_externals[index], strlen(tables.new_externals[index]) + 1)) goto done;
    }
    if (!buffer_put(&out, head + externals_end, end - externals_end)) goto done;
    unsigned long long data_start = data_offset;
    if (20ull + out.size > data_start) data_start = (20ull + out.size + 15) & ~15ull;
    if (data_start > MAX_HEAD_BYTES) {
        log_line("%s: the object table grew too large", text);
        goto done;
    }
    container->real_size = (unsigned long long)size.QuadPart;
    container->real_data_offset = data_offset;
    container->real_end = data_start + (container->real_size - data_offset);
    unsigned long long append = container->real_end;
    int replaced = 0, added = 0;
    for (int pass = 0; pass < 2; pass++) {
        new_index = object_count;
        for (int index = 0; index < container->segment_count; index++) {
            Segment *segment = &container->segments[index];
            int is_new = !segment->found && segment->add_kind;
            if (!segment->size || (!segment->found && !is_new)) continue;
            unsigned int slot = is_new ? entry_out[new_index++] : entry_out[segment->found - 1];
            if (pass == 0 ? is_new : !is_new) continue;
            if (!place_segment(segment, &append, data_start, text)) {
                log_line("%s: an object couldnt be placed, the file is left unmodded", text);
                goto done;
            }
            unsigned int start = (unsigned int)(segment->offset - data_start);
            memcpy(out.data + slot + 8, &start, 4);
            memcpy(out.data + slot + 12, &segment->size, 4);
            if (is_new) added++;
            else replaced++;
        }
    }
    if (!index_segments(container)) {
        log_line("%s: out of memory while indexing the mod objects, the file is left unmodded", text);
        goto done;
    }
    if (append > 0xFFFFFFFFull) {
        log_line("%s: the virtual file would pass 4 GB, left unmodded", text);
        goto done;
    }
    unsigned char *built = calloc(1, (size_t)data_start);
    if (!built) goto done;
    memcpy(built, header, 20);
    memcpy(built + 20, out.data, out.size);
    write_be32(built, (unsigned int)out.size);
    write_be32(built + 4, (unsigned int)append);
    write_be32(built + 12, (unsigned int)data_start);
    container->head = built;
    container->head_size = (unsigned int)data_start;
    container->virtual_size = append;
    log_line("%s: %d replaced, %d added", text, replaced, added);
    result = replaced + added > 0;
    goto done;
malformed:
    log_line("%s: the object table couldnt be parsed", text);
done:
    free(out.data);
    free(entry_at);
    free(entry_out);
    free_tables(&tables);
    free(head);
    real_CloseHandle(file);
    return result;
}

static Container *container_for_path(const wchar_t *path) {
    if (!path || !container_count) return NULL;
    wchar_t normalized[PATH_CHARS];
    if (!normalize_path(path, normalized)) return NULL;
    for (int index = 0; index < container_count; index++) {
        Container *container = &containers[index];
        if (wcscmp(container->full, normalized) != 0) continue;
        EnterCriticalSection(&container->lock);
        if (container->state == 0) container->state = build_container(container) ? 1 : -1;
        LeaveCriticalSection(&container->lock);
        return container->state == 1 ? container : NULL;
    }
    return NULL;
}

static Tracked *find_tracked(HANDLE handle) {
    for (int index = 0; index < MAX_HANDLES; index++) if (tracked[index].handle == handle) return &tracked[index];
    return NULL;
}

static void track(HANDLE handle, Container *container) {
    EnterCriticalSection(&table_lock);
    Tracked *slot = find_tracked(handle);
    if (!slot) slot = find_tracked(NULL);
    if (slot) {
        slot->handle = handle;
        slot->container = container;
        slot->position = 0;
    } else {
        log_line("too many open game files, a handle wasnt tracked");
    }
    LeaveCriticalSection(&table_lock);
}

static int lookup(HANDLE handle, Container **container, unsigned long long *position) {
    if (!handle || handle == INVALID_HANDLE_VALUE) return 0;
    EnterCriticalSection(&table_lock);
    Tracked *slot = find_tracked(handle);
    if (slot) {
        *container = slot->container;
        *position = slot->position;
    }
    LeaveCriticalSection(&table_lock);
    return slot != NULL;
}

static void set_position(HANDLE handle, unsigned long long position) {
    EnterCriticalSection(&table_lock);
    Tracked *slot = find_tracked(handle);
    if (slot) slot->position = position;
    LeaveCriticalSection(&table_lock);
}

static Segment *segment_at(Container *container, unsigned long long offset, unsigned long long *next_start) {
    int low = 0, high = container->order_count;
    while (low < high) {
        int middle = low + (high - low) / 2;
        if (container->segments[container->order[middle]].offset <= offset) low = middle + 1;
        else high = middle;
    }
    *next_start = low < container->order_count ? container->segments[container->order[low]].offset : container->virtual_size;
    if (low > 0) {
        Segment *segment = &container->segments[container->order[low - 1]];
        if (offset < segment->offset + segment->size) return segment;
    }
    return NULL;
}

static int load_segment_memory(Segment *segment) {
    if (segment->memory) return 1;
    if (segment->size > PRELOAD_BYTES) return 0;
    HANDLE file = real_CreateFileW(segment->source, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    unsigned char *memory = malloc(segment->size ? segment->size : 1);
    int ok = memory && read_fully(file, 0, memory, segment->size);
    real_CloseHandle(file);
    if (!ok) {
        free(memory);
        return 0;
    }
    segment->memory = memory;
    return 1;
}

static void apply_patches(Segment *segment, unsigned long long relative, unsigned char *target, unsigned long long take) {
    for (int index = 0; index < segment->patch_count; index++) {
        Patch *patch = &segment->patches[index];
        for (int byte = 0; byte < patch->width; byte++) {
            unsigned long long at = (unsigned long long)patch->offset + byte;
            if (at >= relative && at < relative + take) target[at - relative] = (unsigned char)(patch->value >> (8 * byte));
        }
    }
}

static DWORD compose(Container *container, HANDLE handle, unsigned long long offset, unsigned char *buffer, DWORD size) {
    if (offset >= container->virtual_size) return 0;
    unsigned long long end = offset + size;
    if (end > container->virtual_size) end = container->virtual_size;
    unsigned long long cursor = offset;
    EnterCriticalSection(&container->lock);
    while (cursor < end) {
        unsigned char *target = buffer + (cursor - offset);
        unsigned long long take;
        if (cursor < container->head_size) {
            take = (end < container->head_size ? end : container->head_size) - cursor;
            memcpy(target, container->head + cursor, (size_t)take);
        } else if (cursor < container->real_end) {
            take = (end < container->real_end ? end : container->real_end) - cursor;
            if (!read_fully(handle, cursor - container->head_size + container->real_data_offset, target, take)) memset(target, 0, (size_t)take);
        } else {
            unsigned long long next_start;
            Segment *segment = segment_at(container, cursor, &next_start);
            if (!segment) {
                take = (end < next_start ? end : next_start) - cursor;
                memset(target, 0, (size_t)take);
            } else {
                unsigned long long segment_end = segment->offset + segment->size;
                take = (end < segment_end ? end : segment_end) - cursor;
                if (!segment->memory && segment->file == INVALID_HANDLE_VALUE && segment->source[0]) load_segment_memory(segment);
                if (segment->memory) {
                    memcpy(target, segment->memory + (cursor - segment->offset), (size_t)take);
                    apply_patches(segment, cursor - segment->offset, target, take);
                    cursor += take;
                    continue;
                }
                if (segment->file == INVALID_HANDLE_VALUE) {
                    segment->file = real_CreateFileW(segment->source, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
                }
                if (segment->file == INVALID_HANDLE_VALUE || !read_fully(segment->file, cursor - segment->offset, target, take)) {
                    char source[PATH_CHARS * 3];
                    log_line("mod file %s couldnt be read", narrow(segment->source, source, sizeof(source)));
                    memset(target, 0, (size_t)take);
                } else {
                    apply_patches(segment, cursor - segment->offset, target, take);
                }
            }
        }
        cursor += take;
    }
    LeaveCriticalSection(&container->lock);
    return (DWORD)(end - offset);
}

static HANDLE WINAPI hook_CreateFileW(LPCWSTR name, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES security, DWORD disposition, DWORD flags, HANDLE template_file) {
    HANDLE handle = real_CreateFileW(name, access, share, security, disposition, flags, template_file);
    if (handle == INVALID_HANDLE_VALUE || !name || disposition != OPEN_EXISTING || (access & (GENERIC_WRITE | FILE_WRITE_DATA | FILE_APPEND_DATA))) return handle;
    DWORD error = GetLastError();
    ensure_discovered();
    Container *container = container_for_path(name);
    if (container) track(handle, container);
    SetLastError(error);
    return handle;
}

static BOOL WINAPI hook_ReadFile(HANDLE handle, LPVOID buffer, DWORD size, LPDWORD read, LPOVERLAPPED overlapped) {
    Container *container;
    unsigned long long position;
    if (!lookup(handle, &container, &position)) return real_ReadFile(handle, buffer, size, read, overlapped);
    unsigned long long offset = overlapped ? (((unsigned long long)overlapped->OffsetHigh << 32) | overlapped->Offset) : position;
    if (container->head_size == container->real_data_offset && offset >= container->head_size && offset + size <= container->real_end) {
        if (overlapped) return real_ReadFile(handle, buffer, size, read, overlapped);
        LARGE_INTEGER seek;
        seek.QuadPart = (LONGLONG)offset;
        real_SetFilePointerEx(handle, seek, NULL, FILE_BEGIN);
        DWORD got = 0;
        BOOL ok = real_ReadFile(handle, buffer, size, &got, NULL);
        if (read) *read = got;
        set_position(handle, offset + got);
        return ok;
    }
    DWORD got = compose(container, handle, offset, buffer, size);
    if (read) *read = got;
    if (overlapped) {
        if (got == 0 && size) {
            overlapped->Internal = (ULONG_PTR)0xC0000011L;
            overlapped->InternalHigh = 0;
            if (overlapped->hEvent) SetEvent((HANDLE)((ULONG_PTR)overlapped->hEvent & ~(ULONG_PTR)1));
            SetLastError(ERROR_HANDLE_EOF);
            return FALSE;
        }
        overlapped->Internal = 0;
        overlapped->InternalHigh = got;
        if (overlapped->hEvent) SetEvent((HANDLE)((ULONG_PTR)overlapped->hEvent & ~(ULONG_PTR)1));
    } else {
        set_position(handle, offset + got);
    }
    return TRUE;
}

static int move_position(HANDLE handle, Container *container, unsigned long long position, long long distance, DWORD method, unsigned long long *result) {
    long long base = method == FILE_BEGIN ? 0 : method == FILE_CURRENT ? (long long)position : (long long)container->virtual_size;
    long long target = base + distance;
    if (target < 0) {
        SetLastError(ERROR_NEGATIVE_SEEK);
        return 0;
    }
    *result = (unsigned long long)target;
    set_position(handle, *result);
    LARGE_INTEGER seek;
    seek.QuadPart = target;
    real_SetFilePointerEx(handle, seek, NULL, FILE_BEGIN);
    return 1;
}

static BOOL WINAPI hook_SetFilePointerEx(HANDLE handle, LARGE_INTEGER distance, PLARGE_INTEGER moved, DWORD method) {
    Container *container;
    unsigned long long position, result;
    if (!lookup(handle, &container, &position)) return real_SetFilePointerEx(handle, distance, moved, method);
    if (!move_position(handle, container, position, distance.QuadPart, method, &result)) return FALSE;
    if (moved) moved->QuadPart = (LONGLONG)result;
    return TRUE;
}

static DWORD WINAPI hook_SetFilePointer(HANDLE handle, LONG distance, PLONG high, DWORD method) {
    Container *container;
    unsigned long long position, result;
    if (!lookup(handle, &container, &position)) return real_SetFilePointer(handle, distance, high, method);
    long long full = high ? (long long)(((unsigned long long)(unsigned long)*high << 32) | (unsigned long)distance) : (long long)distance;
    if (!move_position(handle, container, position, full, method, &result)) return INVALID_SET_FILE_POINTER;
    if (high) *high = (LONG)(result >> 32);
    SetLastError(NO_ERROR);
    return (DWORD)result;
}

static DWORD WINAPI hook_GetFileSize(HANDLE handle, LPDWORD high) {
    Container *container;
    unsigned long long position;
    if (!lookup(handle, &container, &position)) return real_GetFileSize(handle, high);
    if (high) *high = (DWORD)(container->virtual_size >> 32);
    SetLastError(NO_ERROR);
    return (DWORD)container->virtual_size;
}

static BOOL WINAPI hook_GetFileInformationByHandle(HANDLE handle, LPBY_HANDLE_FILE_INFORMATION info) {
    BOOL ok = real_GetFileInformationByHandle(handle, info);
    Container *container;
    unsigned long long position;
    if (ok && info && lookup(handle, &container, &position)) {
        info->nFileSizeHigh = (DWORD)(container->virtual_size >> 32);
        info->nFileSizeLow = (DWORD)container->virtual_size;
    }
    return ok;
}

static BOOL WINAPI hook_GetFileAttributesExW(LPCWSTR name, GET_FILEEX_INFO_LEVELS level, LPVOID info) {
    BOOL ok = real_GetFileAttributesExW(name, level, info);
    if (!ok || level != GetFileExInfoStandard || !info) return ok;
    DWORD error = GetLastError();
    ensure_discovered();
    Container *container = container_for_path(name);
    if (container) {
        WIN32_FILE_ATTRIBUTE_DATA *data = info;
        data->nFileSizeHigh = (DWORD)(container->virtual_size >> 32);
        data->nFileSizeLow = (DWORD)container->virtual_size;
    }
    SetLastError(error);
    return ok;
}

static HANDLE WINAPI hook_FindFirstFileW(LPCWSTR name, LPWIN32_FIND_DATAW found) {
    HANDLE search = real_FindFirstFileW(name, found);
    if (search == INVALID_HANDLE_VALUE || !name || wcspbrk(name, L"*?")) return search;
    DWORD error = GetLastError();
    ensure_discovered();
    Container *container = container_for_path(name);
    if (container) {
        found->nFileSizeHigh = (DWORD)(container->virtual_size >> 32);
        found->nFileSizeLow = (DWORD)container->virtual_size;
    }
    SetLastError(error);
    return search;
}

static BOOL WINAPI hook_CloseHandle(HANDLE handle) {
    if (handle && handle != INVALID_HANDLE_VALUE) {
        EnterCriticalSection(&table_lock);
        Tracked *slot = find_tracked(handle);
        if (slot) memset(slot, 0, sizeof(*slot));
        LeaveCriticalSection(&table_lock);
    }
    return real_CloseHandle(handle);
}

typedef struct {
    const char *name;
    void *hook;
    void **original;
} HookEntry;

static int patch_imports(HMODULE module) {
    HookEntry entries[] = {
        {"CreateFileW", hook_CreateFileW, (void **)&real_CreateFileW},
        {"ReadFile", hook_ReadFile, (void **)&real_ReadFile},
        {"SetFilePointerEx", hook_SetFilePointerEx, (void **)&real_SetFilePointerEx},
        {"SetFilePointer", hook_SetFilePointer, (void **)&real_SetFilePointer},
        {"GetFileSize", hook_GetFileSize, (void **)&real_GetFileSize},
        {"GetFileInformationByHandle", hook_GetFileInformationByHandle, (void **)&real_GetFileInformationByHandle},
        {"GetFileAttributesExW", hook_GetFileAttributesExW, (void **)&real_GetFileAttributesExW},
        {"FindFirstFileW", hook_FindFirstFileW, (void **)&real_FindFirstFileW},
        {"CloseHandle", hook_CloseHandle, (void **)&real_CloseHandle},
    };
    int entry_count = sizeof(entries) / sizeof(entries[0]);
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
    for (int index = 0; index < entry_count; index++) {
        *entries[index].original = (void *)GetProcAddress(kernel, entries[index].name);
        if (!*entries[index].original) return 0;
    }
    unsigned char *base = (unsigned char *)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    IMAGE_DATA_DIRECTORY directory = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!directory.VirtualAddress) return 0;
    int patched = 0;
    for (IMAGE_IMPORT_DESCRIPTOR *descriptor = (IMAGE_IMPORT_DESCRIPTOR *)(base + directory.VirtualAddress); descriptor->Name; descriptor++) {
        if (_stricmp((const char *)(base + descriptor->Name), "kernel32.dll") != 0) continue;
        if (!descriptor->OriginalFirstThunk) continue;
        IMAGE_THUNK_DATA *names = (IMAGE_THUNK_DATA *)(base + descriptor->OriginalFirstThunk);
        IMAGE_THUNK_DATA *slots = (IMAGE_THUNK_DATA *)(base + descriptor->FirstThunk);
        for (; names->u1.AddressOfData; names++, slots++) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            IMAGE_IMPORT_BY_NAME *import = (IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData);
            for (int index = 0; index < entry_count; index++) {
                if (strcmp((const char *)import->Name, entries[index].name) != 0) continue;
                DWORD protection;
                if (VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), PAGE_READWRITE, &protection)) {
                    slots->u1.Function = (ULONG_PTR)entries[index].hook;
                    VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), protection, &protection);
                    patched++;
                }
            }
        }
    }
    return patched;
}

static void resolve_folders(void) {
    wchar_t exe[PATH_CHARS];
    DWORD length = GetModuleFileNameW(NULL, exe, PATH_CHARS);
    if (!length || length >= PATH_CHARS) return;
    wchar_t *dot = wcsrchr(exe, L'.');
    if (dot) *dot = 0;
    _snwprintf(data_dir, PATH_CHARS, L"%ls_Data", exe);
    DWORD override_length = GetEnvironmentVariableW(L"SUNFLOWYR_DATA", exe, PATH_CHARS);
    if (override_length && override_length < PATH_CHARS) wcsncpy(data_dir, exe, PATH_CHARS - 1);
    _snwprintf(mods_dir, PATH_CHARS, L"%ls\\Sunflowyr_Mods", data_dir);
    override_length = GetEnvironmentVariableW(L"SUNFLOWYR_MODS", exe, PATH_CHARS);
    if (override_length && override_length < PATH_CHARS) wcsncpy(mods_dir, exe, PATH_CHARS - 1);
}

static void open_log(void) {
    DWORD attributes = GetFileAttributesW(mods_dir);
    if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY)) return;
    wchar_t path[PATH_CHARS];
    if (_snwprintf(path, PATH_CHARS, L"%ls\\sunflowyr_loader.log", mods_dir) < 0) return;
    log_file = _wfopen(path, L"w");
}

static HMODULE real_version;

static FARPROC real_proc(const char *name) {
    if (!real_version) {
        char path[MAX_PATH];
        GetSystemDirectoryA(path, MAX_PATH);
        strncat(path, "\\version.dll", MAX_PATH - strlen(path) - 1);
        real_version = LoadLibraryA(path);
    }
    return real_version ? GetProcAddress(real_version, name) : NULL;
}

#define FORWARD2(name) DWORD WINAPI proxy_##name(DWORD a, DWORD b) { static FARPROC target; if (!target) target = real_proc(#name); return ((DWORD (WINAPI *)(DWORD, DWORD))target)(a, b); }
#define FORWARD3(name) DWORD WINAPI proxy_##name(DWORD a, DWORD b, DWORD c) { static FARPROC target; if (!target) target = real_proc(#name); return ((DWORD (WINAPI *)(DWORD, DWORD, DWORD))target)(a, b, c); }
#define FORWARD4(name) DWORD WINAPI proxy_##name(DWORD a, DWORD b, DWORD c, DWORD d) { static FARPROC target; if (!target) target = real_proc(#name); return ((DWORD (WINAPI *)(DWORD, DWORD, DWORD, DWORD))target)(a, b, c, d); }
#define FORWARD5(name) DWORD WINAPI proxy_##name(DWORD a, DWORD b, DWORD c, DWORD d, DWORD e) { static FARPROC target; if (!target) target = real_proc(#name); return ((DWORD (WINAPI *)(DWORD, DWORD, DWORD, DWORD, DWORD))target)(a, b, c, d, e); }
#define FORWARD8(name) DWORD WINAPI proxy_##name(DWORD a, DWORD b, DWORD c, DWORD d, DWORD e, DWORD f, DWORD g, DWORD h) { static FARPROC target; if (!target) target = real_proc(#name); return ((DWORD (WINAPI *)(DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD))target)(a, b, c, d, e, f, g, h); }

FORWARD4(GetFileVersionInfoA)
FORWARD4(GetFileVersionInfoW)
FORWARD5(GetFileVersionInfoExA)
FORWARD5(GetFileVersionInfoExW)
FORWARD2(GetFileVersionInfoSizeA)
FORWARD2(GetFileVersionInfoSizeW)
FORWARD3(GetFileVersionInfoSizeExA)
FORWARD3(GetFileVersionInfoSizeExW)
FORWARD8(VerFindFileA)
FORWARD8(VerFindFileW)
FORWARD8(VerInstallFileA)
FORWARD8(VerInstallFileW)
FORWARD3(VerLanguageNameA)
FORWARD3(VerLanguageNameW)
FORWARD4(VerQueryValueA)
FORWARD4(VerQueryValueW)

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH && !hooks_installed) {
        DisableThreadLibraryCalls(instance);
        InitializeCriticalSection(&table_lock);
        InitializeCriticalSection(&log_lock);
        resolve_folders();
        open_log();
        int patched = patch_imports(GetModuleHandleW(NULL));
        hooks_installed = 1;
        if (!patched) log_line("the game file functions couldnt be hooked, mods wont load");
    }
    return TRUE;
}
