// Disposable guest-only root-listen, UID-1000-serving echo fixture. No Docker or LSP.
#include <arpa/inet.h>
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static int read_all(int fd, void *buffer, size_t length) {
    unsigned char *bytes = buffer;
    while (length) {
        ssize_t count = read(fd, bytes, length);
        if (count <= 0) return -1;
        bytes += count;
        length -= (size_t)count;
    }
    return 0;
}

static int write_all(int fd, const void *buffer, size_t length) {
    const unsigned char *bytes = buffer;
    while (length) {
        ssize_t count = write(fd, bytes, length);
        if (count <= 0) return -1;
        bytes += count;
        length -= (size_t)count;
    }
    return 0;
}

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

int main(void) {
    const char *path = "/run/doxa-semantic/producer.sock";
    int server = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (server < 0) return 10;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof address.sun_path) return 11;
    strcpy(address.sun_path, path);
    if (bind(server, (const struct sockaddr *)&address, sizeof address) < 0) return 12;
    if (chmod(path, 0666) < 0 || listen(server, 1) < 0) return 13;
    // The listening socket was created under UID 0. Hand its FD to the same
    // process after dropping privilege; SO_PEERCRED may still report UID 0.
    if (setgid(1000) < 0 || setuid(1000) < 0) return 22;
    fprintf(stderr, "BROKER_SERVING_UID=%ld\n", (long)geteuid());
    fflush(stderr);
    int client = accept(server, NULL, NULL);
    if (client < 0) return 14;
    unsigned int size;
    if (read_all(client, &size, 4) < 0) return 15;
    size = ntohl(size);
    if (!size || size > 4096) return 16;
    char request[4097];
    if (read_all(client, request, size) < 0) return 17;
    request[size] = 0;
    if (!strstr(request, "\"operation\":\"observe_only\"")) return 18;
    char nonce[65], digest[65];
    if (field64(request, "nonce", nonce) < 0 || field64(request, "query_sha256", digest) < 0) return 19;
    char response[512];
    int length = snprintf(response, sizeof response,
        "{\"protocol\":\"doxa-semantic-socket-observation-v1\",\"nonce\":\"%s\",\"query_sha256\":\"%s\",\"status\":\"observation_only\"}",
        nonce, digest);
    if (length <= 0 || (size_t)length >= sizeof response) return 20;
    size = htonl((unsigned int)length);
    if (write_all(client, &size, 4) < 0 || write_all(client, response, (size_t)length) < 0) return 21;
    close(client);
    close(server);
    return 0;
}
