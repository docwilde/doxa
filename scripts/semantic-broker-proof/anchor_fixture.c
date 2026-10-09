// Offline guest-only separately controlled reply-sender fixture. No Docker/LSP.
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

static int serve(int listener) {
    fprintf(stderr, "ANCHOR_SERVING_UID=%ld\n", (long)geteuid());
    fflush(stderr);
    int client = accept(listener, NULL, NULL);
    if (client < 0) return 14;
    char request[4097];
    ssize_t count = recv(client, request, sizeof request, 0);
    if (count <= 0 || count > 4096) return 15;
    request[count] = 0;
    if (!strstr(request, "\"operation\":\"observe_only\"")) return 16;
    char nonce[65], digest[65];
    if (field64(request, "nonce", nonce) < 0 || field64(request, "query_sha256", digest) < 0) return 17;
    char reply[512];
    int length = snprintf(reply, sizeof reply,
        "{\"protocol\":\"doxa-semantic-socket-observation-v1\",\"nonce\":\"%s\",\"query_sha256\":\"%s\",\"status\":\"observation_only\"}",
        nonce, digest);
    if (length <= 0 || (size_t)length >= sizeof reply) return 18;
    // One seqpacket carries the entire reply. SCM_CREDENTIALS on the client
    // reports this send's actual process, not the listener's stale identity.
    if (send(client, reply, (size_t)length, MSG_NOSIGNAL) != length) return 19;
    close(client);
    close(listener);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "root") && strcmp(argv[1], "drop")
        && strcmp(argv[1], "handoff"))) return 2;
    const char *path = "/run/doxa-semantic/anchor.sock";
    if (unlink(path) < 0 && errno != ENOENT) return 10;
    int listener = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (listener < 0) return 11;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof address.sun_path) return 12;
    strcpy(address.sun_path, path);
    if (bind(listener, (const struct sockaddr *)&address, sizeof address) < 0
        || chmod(path, 0666) < 0 || listen(listener, 1) < 0) return 13;
    fprintf(stderr, "ANCHOR_MODE=%s LISTENER_UID=%ld\n", argv[1], (long)geteuid());
    fflush(stderr);
    if (!strcmp(argv[1], "handoff")) {
        pid_t child = fork();
        if (child < 0) return 20;
        if (child == 0) {
            if (setgid(1000) < 0 || setuid(1000) < 0) _exit(21);
            _exit(serve(listener));
        }
        close(listener);
        int status = 0;
        if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) return 22;
        return WEXITSTATUS(status);
    }
    if (!strcmp(argv[1], "drop") && (setgid(1000) < 0 || setuid(1000) < 0)) return 23;
    return serve(listener);
}
