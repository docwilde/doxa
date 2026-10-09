// Disposable offline guest only. This is a packet-origin fixture, not Docker.
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

static int field64(const char *json, const char *name, char output[65]) {
    char key[80];
    snprintf(key, sizeof key, "\"%s\":\"", name);
    const char *start = strstr(json, key);
    if (!start) return -1;
    start += strlen(key);
    if (strlen(start) < 65) return -1;
    for (size_t index = 0; index < 64; index++) {
        char byte = start[index];
        if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f'))) return -1;
        output[index] = byte;
    }
    output[64] = 0;
    return start[64] == '"' ? 0 : -1;
}

static int send_packet(int client, const char *text) {
    size_t size = strlen(text);
    return send(client, text, size, MSG_NOSIGNAL) == (ssize_t)size ? 0 : -1;
}

static int rest(int client, const char *mode, const char *nonce, const char *query,
    const char *rust_scan) {
    char packet[1024];
    const char *cid = !strcmp(mode, "cid_swap")
        ? "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        : "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
    snprintf(packet, sizeof packet,
        "{\"protocol\":\"doxa-semantic-stream-observation-v1\",\"nonce\":\"%s\","
        "\"query_sha256\":\"%s\",\"cid\":\"%s\",\"phase\":\"chunk\","
        "\"sequence\":0,\"data_hex\":\"436f6e74656e742d4c656e6774683a20320d0a0d0a7b7d\"}",
        nonce, query, cid);
    // A negative client can close immediately after this packet. The server
    // fixture has already made the adversarial send and need not force more.
    if (send_packet(client, packet) < 0) return 0;
    snprintf(packet, sizeof packet,
        "{\"protocol\":\"doxa-semantic-stream-observation-v1\",\"nonce\":\"%s\","
        "\"query_sha256\":\"%s\",\"cid\":\"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\","
        "\"phase\":\"closed\",\"chunks\":1,"
        "\"stream_sha256\":\"8808496a20662ce284ee12c8544c64bb04c3554629f42b2d3cf628ff21756b7c\","
        "\"rust_scan_input_sha256\":\"%s\",\"status\":\"observation_only\"}",
        nonce, query, rust_scan);
    if (send_packet(client, packet) < 0) return 0;
    if (!strcmp(mode, "extra")) (void)send_packet(client, packet);
    return 0;
}

static int serve(int listener, const char *mode) {
    int client = accept(listener, NULL, NULL);
    if (client < 0) return 14;
    char request[4097];
    ssize_t count = recv(client, request, sizeof request - 1, 0);
    if (count <= 0 || count > 4096) return 15;
    request[count] = 0;
    if (!strstr(request, "\"operation\":\"observe_stream\"")) return 16;
    char nonce[65], query[65], source[65], target[65], rust_scan[65];
    if (field64(request, "nonce", nonce) < 0 || field64(request, "query_sha256", query) < 0
        || field64(request, "source_sha256", source) < 0
        || field64(request, "target_sha256", target) < 0
        || field64(request, "rust_scan_input_sha256", rust_scan) < 0) return 17;
    char packet[1024];
    snprintf(packet, sizeof packet,
        "{\"protocol\":\"doxa-semantic-stream-observation-v1\",\"nonce\":\"%s\","
        "\"query_sha256\":\"%s\",\"cid\":\"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\","
        "\"phase\":\"opened\",\"source_sha256\":\"%s\",\"target_sha256\":\"%s\","
        "\"rust_scan_input_sha256\":\"%s\","
        "\"image_id\":\"sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff\","
        "\"status\":\"observation_only\"}", nonce, query, source, target, rust_scan);
    if (send_packet(client, packet) < 0) return 18;
    if (!strcmp(mode, "mid_handoff") || !strcmp(mode, "root_switch")) {
        pid_t child = fork();
        if (child < 0) return 19;
        if (child == 0) {
            if (!strcmp(mode, "mid_handoff") && (setgid(1000) < 0 || setuid(1000) < 0)) _exit(20);
            _exit(rest(client, mode, nonce, query, rust_scan));
        }
        close(client);
        int status = 0;
        if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) return 21;
        return WEXITSTATUS(status);
    }
    int status = rest(client, mode, nonce, query, rust_scan);
    close(client);
    return status;
}

int main(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "root") && strcmp(argv[1], "handoff")
        && strcmp(argv[1], "mid_handoff") && strcmp(argv[1], "root_switch")
        && strcmp(argv[1], "cid_swap") && strcmp(argv[1], "extra"))) return 2;
    const char *path = "/run/doxa-semantic/stream.sock";
    if (unlink(path) < 0 && errno != ENOENT) return 10;
    int listener = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (listener < 0) return 11;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof address.sun_path) return 12;
    strcpy(address.sun_path, path);
    if (bind(listener, (const struct sockaddr *)&address, sizeof address) < 0
        || chmod(path, 0666) < 0 || listen(listener, 1) < 0) return 13;
    fprintf(stderr, "STREAM_MODE=%s LISTENER_UID=%ld\n", argv[1], (long)geteuid());
    fflush(stderr);
    if (!strcmp(argv[1], "handoff")) {
        pid_t child = fork();
        if (child < 0) return 22;
        if (child == 0) {
            if (setgid(1000) < 0 || setuid(1000) < 0) _exit(23);
            _exit(serve(listener, argv[1]));
        }
        close(listener);
        int status = 0;
        if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) return 24;
        return WEXITSTATUS(status);
    }
    int status = serve(listener, argv[1]);
    close(listener);
    return status;
}
